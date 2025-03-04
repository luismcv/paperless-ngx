import os
import re
import tempfile
from datetime import date
from datetime import datetime
from datetime import timedelta
from fnmatch import fnmatch
from typing import Dict

import magic
import pathvalidate
from django.conf import settings
from django.db import DatabaseError
from documents.loggers import LoggingMixin
from documents.models import Correspondent
from documents.parsers import is_mime_type_supported
from documents.tasks import consume_file
from imap_tools import AND
from imap_tools import MailBox
from imap_tools import MailboxFolderSelectError
from imap_tools import MailBoxUnencrypted
from imap_tools import MailMessage
from imap_tools import MailMessageFlags
from imap_tools import NOT
from imap_tools.mailbox import MailBoxTls
from paperless_mail.models import MailAccount
from paperless_mail.models import MailRule


class MailError(Exception):
    pass


class BaseMailAction:
    def get_criteria(self) -> Dict:
        return {}

    def post_consume(self, M, message_uids, parameter):
        pass  # pragma: nocover


class DeleteMailAction(BaseMailAction):
    def post_consume(self, M, message_uids, parameter):
        M.delete(message_uids)


class MarkReadMailAction(BaseMailAction):
    def get_criteria(self):
        return {"seen": False}

    def post_consume(self, M, message_uids, parameter):
        M.flag(message_uids, [MailMessageFlags.SEEN], True)


class MoveMailAction(BaseMailAction):
    def post_consume(self, M, message_uids, parameter):
        M.move(message_uids, parameter)


class FlagMailAction(BaseMailAction):
    def get_criteria(self):
        return {"flagged": False}

    def post_consume(self, M, message_uids, parameter):
        M.flag(message_uids, [MailMessageFlags.FLAGGED], True)


class TagMailAction(BaseMailAction):
    def __init__(self, parameter):
        self.keyword = parameter

    def get_criteria(self):
        return {"no_keyword": self.keyword, "gmail_label": self.keyword}

    def post_consume(self, M: MailBox, message_uids, parameter):
        if re.search(r"gmail\.com$|googlemail\.com$", M._host):
            for uid in message_uids:
                M.client.uid("STORE", uid, "+X-GM-LABELS", self.keyword)
        else:
            M.flag(message_uids, [self.keyword], True)


def get_rule_action(rule) -> BaseMailAction:
    if rule.action == MailRule.MailAction.FLAG:
        return FlagMailAction()
    elif rule.action == MailRule.MailAction.DELETE:
        return DeleteMailAction()
    elif rule.action == MailRule.MailAction.MOVE:
        return MoveMailAction()
    elif rule.action == MailRule.MailAction.MARK_READ:
        return MarkReadMailAction()
    elif rule.action == MailRule.MailAction.TAG:
        return TagMailAction(rule.action_parameter)
    else:
        raise NotImplementedError("Unknown action.")  # pragma: nocover


def make_criterias(rule):
    maximum_age = date.today() - timedelta(days=rule.maximum_age)
    criterias = {}
    if rule.maximum_age > 0:
        criterias["date_gte"] = maximum_age
    if rule.filter_from:
        criterias["from_"] = rule.filter_from
    if rule.filter_subject:
        criterias["subject"] = rule.filter_subject
    if rule.filter_body:
        criterias["body"] = rule.filter_body

    return {**criterias, **get_rule_action(rule).get_criteria()}


def get_mailbox(server, port, security) -> MailBox:
    if security == MailAccount.ImapSecurity.NONE:
        mailbox = MailBoxUnencrypted(server, port)
    elif security == MailAccount.ImapSecurity.STARTTLS:
        mailbox = MailBoxTls(server, port)
    elif security == MailAccount.ImapSecurity.SSL:
        mailbox = MailBox(server, port)
    else:
        raise NotImplementedError("Unknown IMAP security")  # pragma: nocover
    return mailbox


class MailAccountHandler(LoggingMixin):

    logging_name = "paperless_mail"

    def _correspondent_from_name(self, name):
        try:
            return Correspondent.objects.get_or_create(name=name)[0]
        except DatabaseError as e:
            self.log("error", f"Error while retrieving correspondent {name}: {e}")
            return None

    def get_title(self, message, att, rule):
        if rule.assign_title_from == MailRule.TitleSource.FROM_SUBJECT:
            return message.subject

        elif rule.assign_title_from == MailRule.TitleSource.FROM_FILENAME:
            return os.path.splitext(os.path.basename(att.filename))[0]

        else:
            raise NotImplementedError(
                "Unknown title selector.",
            )  # pragma: nocover

    def get_date(self, message, att, rule):
        if rule.assign_date_from == MailRule.DateSource.FROM_EMAIL_DATE:
            return message.date

        elif rule.assign_date_from == MailRule.DateSource.FROM_ATTACHMENT_PARSING:
            return None

        else:
            raise NotImplementedError(
                "Unknown title selector.",
            )  # pragma: nocover

    def get_correspondent(self, message: MailMessage, rule):
        c_from = rule.assign_correspondent_from

        if c_from == MailRule.CorrespondentSource.FROM_NOTHING:
            return None

        elif c_from == MailRule.CorrespondentSource.FROM_EMAIL:
            return self._correspondent_from_name(message.from_)

        elif c_from == MailRule.CorrespondentSource.FROM_NAME:
            from_values = message.from_values
            if from_values is not None and len(from_values.name) > 0:
                return self._correspondent_from_name(from_values.name)
            else:
                return self._correspondent_from_name(message.from_)

        elif c_from == MailRule.CorrespondentSource.FROM_CUSTOM:
            return rule.assign_correspondent

        else:
            raise NotImplementedError(
                "Unknown correspondent selector",
            )  # pragma: nocover

    def handle_mail_account(self, account: MailAccount):

        self.renew_logging_group()

        self.log("debug", f"Processing mail account {account}")

        total_processed_files = 0
        try:
            with get_mailbox(
                account.imap_server,
                account.imap_port,
                account.imap_security,
            ) as M:

                supports_gmail_labels = "X-GM-EXT-1" in M.client.capabilities
                supports_auth_plain = "AUTH=PLAIN" in M.client.capabilities

                self.log("debug", f"GMAIL Label Support: {supports_gmail_labels}")
                self.log("debug", f"AUTH=PLAIN Support: {supports_auth_plain}")

                try:

                    M.login(account.username, account.password)

                except UnicodeEncodeError:
                    self.log("debug", "Falling back to AUTH=PLAIN")

                    try:
                        M.login_utf8(account.username, account.password)
                    except Exception as err:
                        self.log(
                            "error",
                            "Unable to authenticate with mail server using AUTH=PLAIN",
                        )
                        raise MailError(
                            f"Error while authenticating account {account}",
                        ) from err
                except Exception as e:
                    self.log(
                        "error",
                        f"Error while authenticating account {account}: {e}",
                        exc_info=False,
                    )
                    raise MailError(
                        f"Error while authenticating account {account}",
                    ) from e

                self.log(
                    "debug",
                    f"Account {account}: Processing "
                    f"{account.rules.count()} rule(s)",
                )

                for rule in account.rules.order_by("order"):
                    try:
                        total_processed_files += self.handle_mail_rule(
                            M,
                            rule,
                            supports_gmail_labels,
                        )
                    except Exception as e:
                        self.log(
                            "error",
                            f"Rule {rule}: Error while processing rule: {e}",
                            exc_info=True,
                        )
        except MailError:
            raise
        except Exception as e:
            self.log(
                "error",
                f"Error while retrieving mailbox {account}: {e}",
                exc_info=False,
            )

        return total_processed_files

    def handle_mail_rule(
        self,
        M: MailBox,
        rule: MailRule,
        supports_gmail_labels: bool = False,
    ):

        self.log("debug", f"Rule {rule}: Selecting folder {rule.folder}")

        try:
            M.folder.set(rule.folder)
        except MailboxFolderSelectError as err:

            self.log(
                "error",
                f"Unable to access folder {rule.folder}, attempting folder listing",
            )
            try:
                for folder_info in M.folder.list():
                    self.log("info", f"Located folder: {folder_info.name}")
            except Exception as e:
                self.log(
                    "error",
                    "Exception during folder listing, unable to provide list folders: "
                    + str(e),
                )

            raise MailError(
                f"Rule {rule}: Folder {rule.folder} "
                f"does not exist in account {rule.account}",
            ) from err

        criterias = make_criterias(rule)

        # Deal with the Gmail label extension
        if "gmail_label" in criterias:

            gmail_label = criterias["gmail_label"]
            del criterias["gmail_label"]

            if not supports_gmail_labels:
                criterias_imap = AND(gmail_label=rule.filter_label, **criterias)
            else:
                criterias_imap = AND(
                    NOT(gmail_label=gmail_label),
                    gmail_label=rule.filter_label,
                    **criterias,
                )
        else:
            criterias_imap = AND(gmail_label=rule.filter_label, **criterias)

        self.log(
            "debug",
            f"Rule {rule}: Searching folder with criteria " f"{str(criterias_imap)}",
        )

        try:
            messages = M.fetch(
                criteria=criterias_imap,
                mark_seen=False,
                charset=rule.account.character_set,
            )
        except Exception as err:
            raise MailError(
                f"Rule {rule}: Error while fetching folder {rule.folder}",
            ) from err

        post_consume_messages = []

        mails_processed = 0
        total_processed_files = 0

        for message in messages:
            try:
                processed_files = self.handle_message(message, rule)
                if processed_files > 0:
                    post_consume_messages.append(message.uid)

                total_processed_files += processed_files
                mails_processed += 1
            except Exception as e:
                self.log(
                    "error",
                    f"Rule {rule}: Error while processing mail " f"{message.uid}: {e}",
                    exc_info=True,
                )

        self.log("debug", f"Rule {rule}: Processed {mails_processed} matching mail(s)")

        self.log(
            "debug",
            f"Rule {rule}: Running mail actions on "
            f"{len(post_consume_messages)} mails",
        )

        try:
            get_rule_action(rule).post_consume(
                M,
                post_consume_messages,
                rule.action_parameter,
            )

        except Exception as e:
            raise MailError(
                f"Rule {rule}: Error while processing post-consume actions: " f"{e}",
            ) from e

        return total_processed_files

    def handle_message(self, message, rule: MailRule) -> int:
        processed_elements = 0

        # Skip Message handling when only attachments are to be processed but
        # message doesn't have any.
        if (
            not message.attachments
            and rule.consumption_scope == MailRule.ConsumptionScope.ATTACHMENTS_ONLY
        ):
            return processed_elements

        self.log(
            "debug",
            f"Rule {rule}: "
            f"Processing mail {message.subject} from {message.from_} with "
            f"{len(message.attachments)} attachment(s)",
        )

        correspondent = self.get_correspondent(message, rule)
        tag_ids = [tag.id for tag in rule.assign_tags.all()]
        doc_type = rule.assign_document_type

        if (
            rule.consumption_scope == MailRule.ConsumptionScope.EML_ONLY
            or rule.consumption_scope == MailRule.ConsumptionScope.EVERYTHING
        ):
            processed_elements += self.process_eml(
                message,
                rule,
                correspondent,
                tag_ids,
                doc_type,
            )

        if (
            rule.consumption_scope == MailRule.ConsumptionScope.ATTACHMENTS_ONLY
            or rule.consumption_scope == MailRule.ConsumptionScope.EVERYTHING
        ):
            processed_elements += self.process_attachments(
                message,
                rule,
                correspondent,
                tag_ids,
                doc_type,
            )

        return processed_elements

    def process_attachments(
        self,
        message: MailMessage,
        rule: MailRule,
        correspondent,
        tag_ids,
        doc_type,
    ):
        processed_attachments = 0
        for att in message.attachments:

            if (
                not att.content_disposition == "attachment"
                and rule.attachment_type
                == MailRule.AttachmentProcessing.ATTACHMENTS_ONLY
            ):
                self.log(
                    "debug",
                    f"Rule {rule}: "
                    f"Skipping attachment {att.filename} "
                    f"with content disposition {att.content_disposition}",
                )
                continue

            if rule.filter_attachment_filename:
                # Force the filename and pattern to the lowercase
                # as this is system dependent otherwise
                if not fnmatch(
                    att.filename.lower(),
                    rule.filter_attachment_filename.lower(),
                ):
                    continue

            title = self.get_title(message, att, rule)

            date = self.get_date(message, att, rule)

            # don't trust the content type of the attachment. Could be
            # generic application/octet-stream.
            mime_type = magic.from_buffer(att.payload, mime=True)

            if is_mime_type_supported(mime_type):

                os.makedirs(settings.SCRATCH_DIR, exist_ok=True)
                _, temp_filename = tempfile.mkstemp(
                    prefix="paperless-mail-",
                    dir=settings.SCRATCH_DIR,
                )
                with open(temp_filename, "wb") as f:
                    f.write(att.payload)

                self.log(
                    "info",
                    f"Rule {rule}: "
                    f"Consuming attachment {att.filename} from mail "
                    f"{message.subject} from {message.from_}",
                )

                self.log(
                    "info",
                    f"Email Date {date}: ",
                )

                created = None if date is None else datetime.isoformat(date)

                self.log(
                    "info",
                    f"Created {created}: ",
                )

                consume_file.delay(
                    path=temp_filename,
                    override_filename=pathvalidate.sanitize_filename(
                        att.filename,
                    ),
                    override_title=title,
                    override_created=created,
                    override_correspondent_id=correspondent.id
                    if correspondent
                    else None,
                    override_document_type_id=doc_type.id if doc_type else None,
                    override_tag_ids=tag_ids,
                )

                processed_attachments += 1
            else:
                self.log(
                    "debug",
                    f"Rule {rule}: "
                    f"Skipping attachment {att.filename} "
                    f"since guessed mime type {mime_type} is not supported "
                    f"by paperless",
                )
        return processed_attachments

    def process_eml(
        self,
        message: MailMessage,
        rule: MailRule,
        correspondent,
        tag_ids,
        doc_type,
    ):
        os.makedirs(settings.SCRATCH_DIR, exist_ok=True)
        _, temp_filename = tempfile.mkstemp(
            prefix="paperless-mail-",
            dir=settings.SCRATCH_DIR,
            suffix=".eml",
        )
        with open(temp_filename, "wb") as f:
            # Move "From"-header to beginning of file
            # TODO: This ugly workaround is needed because the parser is
            #   chosen only by the mime_type detected via magic
            #   (see documents/consumer.py "mime_type = magic.from_file")
            #   Unfortunately magic sometimes fails to detect the mime
            #   type of .eml files correctly as message/rfc822 and instead
            #   detects text/plain.
            #   This also effects direct file consumption of .eml files
            #   which are not treated with this workaround.
            from_element = None
            for i, header in enumerate(message.obj._headers):
                if header[0] == "From":
                    from_element = i
            if from_element:
                new_headers = [message.obj._headers.pop(from_element)]
                new_headers += message.obj._headers
                message.obj._headers = new_headers

            f.write(message.obj.as_bytes())

        self.log(
            "info",
            f"Rule {rule}: "
            f"Consuming eml from mail "
            f"{message.subject} from {message.from_}",
        )

        consume_file.delay(
            path=temp_filename,
            override_filename=pathvalidate.sanitize_filename(
                message.subject + ".eml",
            ),
            override_title=message.subject,
            override_correspondent_id=correspondent.id if correspondent else None,
            override_document_type_id=doc_type.id if doc_type else None,
            override_tag_ids=tag_ids,
        )
        processed_elements = 1
        return processed_elements

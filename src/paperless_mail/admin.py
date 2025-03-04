from django import forms
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from paperless_mail.models import MailAccount
from paperless_mail.models import MailRule


class MailAccountAdminForm(forms.ModelForm):

    """Metadata classes used by Django admin to display the form."""

    class Meta:

        """Metadata class used by Django admin to display the form."""

        model = MailAccount
        widgets = {
            "password": forms.PasswordInput(),
        }
        fields = "__all__"


class MailAccountAdmin(admin.ModelAdmin):

    list_display = ("name", "imap_server", "username")

    fieldsets = [
        (None, {"fields": ["name", "imap_server", "imap_port"]}),
        (_("Authentication"), {"fields": ["imap_security", "username", "password"]}),
        (_("Advanced settings"), {"fields": ["character_set"]}),
    ]
    form = MailAccountAdminForm


class MailRuleAdmin(admin.ModelAdmin):

    radio_fields = {
        "attachment_type": admin.VERTICAL,
        "action": admin.VERTICAL,
        "assign_title_from": admin.VERTICAL,
        "assign_date_from": admin.VERTICAL,
        "assign_correspondent_from": admin.VERTICAL,
    }

    fieldsets = (
        (None, {"fields": ("name", "order", "account", "folder")}),
        (
            _("Filter"),
            {
                "description": _(
                    "Paperless will only process mails that match ALL of the "
                    "filters given below.",
                ),
                "fields": (
                    "filter_from",
                    "filter_subject",
                    "filter_body",
                    "filter_attachment_filename",
                    "filter_label",
                    "maximum_age",
                    "consumption_scope",
                    "attachment_type",
                ),
            },
        ),
        (
            _("Actions"),
            {
                "description": _(
                    "The action applied to the mail. This action is only "
                    "performed when the mail body or attachments were "
                    "consumed from the mail.",
                ),
                "fields": ("action", "action_parameter"),
            },
        ),
        (
            _("Metadata"),
            {
                "description": _(
                    "Assign metadata to documents consumed from this rule "
                    "automatically. If you do not assign tags, types or "
                    "correspondents here, paperless will still process all "
                    "matching rules that you have defined.",
                ),
                "fields": (
                    "assign_title_from",
                    "assign_date_from",
                    "assign_tags",
                    "assign_document_type",
                    "assign_correspondent_from",
                    "assign_correspondent",
                ),
            },
        ),
    )

    list_filter = ("account",)

    list_display = ("order", "name", "account", "folder", "action")

    list_editable = ("order",)

    list_display_links = ("name",)

    sortable_by = []

    ordering = ["order"]


admin.site.register(MailAccount, MailAccountAdmin)
admin.site.register(MailRule, MailRuleAdmin)

# -*- coding: utf-8 -*-
##############################################################################
#
#    EQP Solutions
#
#    Copyright (C) 2024-TODAY EQP Solutions (<https://www.eqpsolutions.com>)
#    Author: EQP Solutions (<info@eqpsolutions.com>)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with this program. If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

import os
import io
import json
import base64
import logging

from odoo import api, models, fields, _
from odoo.exceptions import UserError, ValidationError
from datetime import datetime, timezone

# paramiko is declared in the manifest `external_dependencies`
from paramiko import Transport, SFTPClient, AuthenticationException

_logger = logging.getLogger(__name__)

# Constants Declaration
SCOPES = ["https://www.googleapis.com/auth/drive"]
# Credentials are only readable by the backup admins
CREDENTIALS_GROUP = "eqp_backups.group_eqp_backups_admin"


# Static Functions


def get_credentials_data(credentials_content):
    """
    Parse and return credentials data from the given content.

    Args:
        credentials_content (str): Content of the credential file.

    Returns:
        dict or str: Parsed credentials data.
    """
    try:
        # Assuming the content is a dictionary
        credentials_data = json.loads(credentials_content)
    except json.JSONDecodeError as e:
        # Handle the case where the file is not a valid JSON
        raise ValidationError(_("Invalid JSON format. Error: %s", e))
    return credentials_data


# Model `BackupServer`
class BackupServer(models.Model):
    """
    Model to manage backup servers and their configurations.
    """

    _name = "backup.server"
    _description = "Backup Servers"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "name"

    active = fields.Boolean(
        default=True,
        tracking=True,
        string="Active",
        copy=False,
        help="Set active to false to hide the record without removing it.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        index=True,
        default=lambda self: self.env.company,
        readonly=True,
    )

    name = fields.Char(string="Name", required=True, tracking=True, help="Record Name")
    description = fields.Text(
        string="Description", copy=False, help="Add a brief description of the record"
    )
    state = fields.Selection(
        [
            ("draft", "Not Confirmed"),
            ("confirmed", "Confirmed"),
            ("archived", "Archived"),
        ],
        string="Status",
        default="draft",
        readonly=True,
        tracking=True,
        copy=False,
        help="Backup Server Status",
    )

    backup_type = fields.Selection(
        [
            ("local", "Local"),
            ("sftp", "SFTP"),
            ("drive", "Google Drive"),
            ("dropbox", "Dropbox"),
        ],
        string="Backup Type",
        copy=False,
        tracking=True,
        help="Select the Backup type:"
        "\nLocal: Save in the local server."
        "\nSFTP: Send the backup to an SFTP server."
        "\nG. Drive: Send to a Google Drive Folder."
        "\nDropbox: Send to a Dropbox Folder.",
    )
    destination_path = fields.Char(
        string="Destination Path",
        tracking=True,
        copy=False,
        help="Put the full backup destination path route.",
    )
    # SFTP SERVER FIELDS
    server_address = fields.Char(string="Address", tracking=True, help="Server Address")
    server_port = fields.Char(string="Port", tracking=True, size=5, help="Server Port")
    server_user = fields.Char(string="User", tracking=True, help="Server User")
    server_password = fields.Char(
        string="Password",
        copy=False,
        groups=CREDENTIALS_GROUP,
        help="Server Password",
    )
    # GOOGLE DRIVE FIELDS
    drive_credentials_type = fields.Selection(
        [("file", "Upload Credentials File"), ("text", "Enter Credentials Directly")],
        string="Credentials Type",
        tracking=True,
        help="Choose the type of input for save your Credentials:\n"
        "- Upload Credentials File: Select and upload your service account key "
        "file in .json format for G. Drive. Or .txt for Dropbox.\n"
        "- Enter Credentials Directly: Manually type the credentials directly into"
        " a text field, respecting the JSON structure format for G. Drive. "
        "Or plain text for Dropbox .",
    )
    drive_credentials_file = fields.Binary(
        string="Credentials File",
        attachment=True,
        copy=False,
        groups=CREDENTIALS_GROUP,
        help="Upload your service account key file:\n"
        "- .json extension for Google Drive.\n"
        "- .txt extension for Dropbox",
    )
    drive_credentials_file_name = fields.Char(string="Credentials File Name")
    drive_credentials_input = fields.Text(
        string="Credentials Input",
        copy=False,
        groups=CREDENTIALS_GROUP,
        help="Enter your service account key directly:\n"
        "- JSON format for Google Drive.\n"
        "- Plain Text for Dropbox.",
    )
    # DROPBOX FIELDS
    dropbox_app_key = fields.Char(
        string="App Key",
        copy=False,
        groups=CREDENTIALS_GROUP,
        help="Dropbox APP Key",
    )
    dropbox_app_secret = fields.Char(
        string="App Secret",
        copy=False,
        groups=CREDENTIALS_GROUP,
        help="Dropbox APP Secret",
    )
    dropbox_app_token = fields.Char(
        string="App Token",
        readonly=True,
        copy=False,
        groups=CREDENTIALS_GROUP,
        help="Dropbox APP Token",
    )

    parent_folder = fields.Char(
        string="Parent Folder ID",
        tracking=True,
        help="Parent Folder ID.\n(to get this ID open the folder on Google Drive and copy it "
        "from the web URL)",
    )
    credentials_ok = fields.Boolean(
        string="Credentials Validation",
        compute="_compute_credentials_ok",
        compute_sudo=True,
    )
    record_ids = fields.One2many("backup.record", "server_id", string="Related Records")

    _name_unique = models.Constraint(
        "unique(name, company_id)", "A unique name per company."
    )

    # Constraints
    @api.constrains("server_port")
    def _check_server_port(self):
        for server in self:
            port = server.server_port
            if port and not (port.isdigit() and 0 < int(port) < 65536):
                raise ValidationError(
                    _("The Port must be a number between 1 and 65535.")
                )

    def write(self, vals):
        """
        Overrides the write method to update the state based on the 'active' field.

        Args:
            vals (dict): Values to write.

        Returns:
            dict: Written values.
        """
        # Update State.
        if "active" in vals:
            if vals["active"]:
                vals = dict(vals, state="draft")
            else:
                vals = dict(vals, state="archived")
                # An archived server can not run backups anymore
                self._pause_records()
        return super(BackupServer, self).write(vals)

    def copy(self, default=None):
        self.ensure_one()
        chosen_name = default.get("name") if default else ""
        new_name = chosen_name or _("%s (copy)", self.name)
        default = dict(default or {}, name=new_name)
        return super(BackupServer, self).copy(default)

    @api.depends(
        "backup_type",
        "destination_path",
        "server_address",
        "server_user",
        "server_port",
        "server_password",
        "parent_folder",
        "drive_credentials_type",
        "drive_credentials_file",
        "drive_credentials_input",
        "dropbox_app_key",
        "dropbox_app_secret",
        "dropbox_app_token",
    )
    def _compute_credentials_ok(self):
        """
        Computes the value of 'credentials_ok' field based on the backup type and associated credentials.
        """
        for server in self:
            credentials_ok = False
            backup_type = server.backup_type

            if backup_type == "local":
                credentials_ok = bool(server.destination_path)
            elif backup_type == "sftp":
                credentials_ok = bool(
                    server.server_address
                    and server.server_user
                    and server.server_port
                    and server.server_password
                )
            elif backup_type == "drive":
                credentials_ok = bool(
                    server.parent_folder
                    and (
                        (
                            server.drive_credentials_type == "file"
                            and server.drive_credentials_file
                        )
                        or (
                            server.drive_credentials_type == "text"
                            and server.drive_credentials_input
                        )
                    )
                )
            elif backup_type == "dropbox":
                credentials_ok = bool(
                    server.dropbox_app_key
                    and server.dropbox_app_secret
                    and server.dropbox_app_token
                )

            server.credentials_ok = credentials_ok

    def _pause_records(self):
        """Pause the confirmed records (and their crons) of the servers."""
        records = self.record_ids.filtered(lambda r: r.state == "confirmed")
        if records:
            records.update_cron_state(False)
            records.write({"state": "paused"})

    def revert_state(self):
        """
        Reverts the state of the server and associated records to 'draft'.
        """
        for server in self:
            if server.state == "confirmed":
                server.write({"state": "draft"})
                server._pause_records()

    def check_company_policy(self):
        """
        Checks company policy for enabling backup types.

        Raises:
            UserError: If the policy is not enabled for the selected backup type.
        """
        for server in self:
            company_id = server.company_id or self.env.company
            backup_type = server.backup_type
            if backup_type and not company_id[f"eqp_backups_enable_{backup_type}"]:
                selection = self._fields["backup_type"]._description_selection(self.env)
                raise UserError(
                    _(
                        "You cannot use this functionality without the %s policy "
                        "enabled.",
                        dict(selection)[backup_type],
                    )
                )

    @api.onchange("backup_type")
    def _onchange_backup_type(self):
        """
        Onchange method to validate a backup type based on company policies.
        """
        self.check_company_policy()

    def confirm(self):
        """
        Confirms the server, updating the state and associated records.
        """
        for server in self:
            server.check_company_policy()
            # Update State to confirm
            server.write({"state": "confirmed"})
            records = server.record_ids.filtered(lambda r: r.state == "paused")
            if records:
                records.update_cron_state(True)
                records.write({"state": "confirmed"})

    def generate_dropbox_token(self):
        """
        Generates a Dropbox token using the provided credentials.

        Returns:
            dict: Action dictionary to open the Dropbox Token Assignment wizard.

        Raises:
            UserError: If the credentials are missing or if an error occurs during token generation.
        """
        self.ensure_one()

        dropbox_app_key = self.dropbox_app_key
        dropbox_app_secret = self.dropbox_app_secret
        if not dropbox_app_key or not dropbox_app_secret:
            raise UserError(
                _(
                    "Error: Missing credentials. Please ensure that all the following "
                    "credentials are set: APP Key and Secret."
                )
            )
        try:
            import dropbox

            dbx_auth = dropbox.oauth.DropboxOAuth2FlowNoRedirect(
                dropbox_app_key, dropbox_app_secret, token_access_type="offline"
            )
            dbx_auth_url = dbx_auth.start()
        except Exception as e:
            raise UserError(_("Error: %s.", e))

        return {
            "type": "ir.actions.act_window",
            "name": _("Dropbox Token Assignment"),
            "res_model": "backup.dropbox.token.assignment.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_dropbox_auth_url": dbx_auth_url,
                "default_server_id": self.id,
            },
        }

    def establish_sftp_connection(self):
        """
        Establishes an SFTP connection to the server.

        Returns:
            tuple: Tuple containing SFTP connection and transport objects.
        """
        self.ensure_one()
        # Establish an SFTP connection
        transport = Transport((self.server_address, int(self.server_port)))
        try:
            transport.connect(username=self.server_user, password=self.server_password)
            sftp_connection = SFTPClient.from_transport(transport)
        except Exception:
            transport.close()
            raise

        return sftp_connection, transport

    def get_file_path_details(self, name, extension):
        """
        Validates the extension and formats file path details.

        Args:
            name (str): Name of the file.
            extension (str): File extension.

        Returns:
            tuple: Tuple containing destination path and formatted file name.
        """
        # Validate the extension
        if extension not in ("txt", "zip"):
            _logger.error("Unsupported file extension (supported formats: txt, zip)")
            raise ValidationError(
                _("Unsupported file extension (supported formats: txt, zip)")
            )
        # Catching and formatting timestamp
        now = fields.Datetime.context_timestamp(self, fields.Datetime.now())
        formatted_date = now.strftime("%Y-%m-%d_%H.%M.%S")
        file_name = f"Backup_{name}_{formatted_date}.{extension}"
        destination_path = (
            self.destination_path
            if self.destination_path and self.destination_path.endswith("/")
            else (self.destination_path or "") + "/"
        )

        return destination_path, file_name

    def get_drive_file_media(self, f_content, m_type):
        """
        Returns a MediaIoBaseUpload object for Google Drive file upload.

        Args:
            f_content (io.BytesIO): File-like object containing the file content.
            m_type (str): Mime type of the file.

        Returns:
            MediaIoBaseUpload: Media upload object.
        """
        from googleapiclient.http import MediaIoBaseUpload

        return MediaIoBaseUpload(f_content, mimetype=m_type, resumable=True)

    def _get_drive_credentials_data(self):
        """Return the parsed Google Drive service account key."""
        self.ensure_one()
        # File Upload
        if self.drive_credentials_type == "file":
            if not self.drive_credentials_file:
                raise UserError(_("Error, No credentials file uploaded"))
            return get_credentials_data(base64.b64decode(self.drive_credentials_file))
        # Text Input
        if self.drive_credentials_type == "text" and self.drive_credentials_input:
            return get_credentials_data(self.drive_credentials_input)
        raise UserError(_("Error, No valid or empty credentials text input"))

    def provider_authenticate(self):
        """Authenticate with the backup provider and return the service object.

        Returns:
            object: Service object for the backup provider.
        """
        self.ensure_one()
        backup_type = self.backup_type

        # Validate type
        if backup_type not in ("drive", "dropbox"):
            raise UserError(_("Error, Invalid backup type specified."))

        if backup_type == "drive":
            credentials_data = self._get_drive_credentials_data()
        elif (
            self.dropbox_app_key and self.dropbox_app_secret and self.dropbox_app_token
        ):
            credentials_data = (
                self.dropbox_app_key,
                self.dropbox_app_secret,
                self.dropbox_app_token,
            )
        else:
            raise UserError(
                _(
                    "Error: Missing or empty credentials. Please ensure that all the "
                    "following credentials are set: APP Key, Secret, and Token."
                )
            )
        # Get Service
        try:
            if backup_type == "drive":
                from google.oauth2 import service_account
                from googleapiclient.discovery import build

                creds = service_account.Credentials.from_service_account_info(
                    credentials_data, scopes=SCOPES
                )
                service = build("drive", "v3", credentials=creds)
            else:
                import dropbox

                service = dropbox.Dropbox(
                    app_key=credentials_data[0],
                    app_secret=credentials_data[1],
                    oauth2_refresh_token=credentials_data[2],
                )
        except Exception as e:
            raise UserError(
                _(
                    "Failed to initialize %(type)s service. Error: %(error)s",
                    type=backup_type,
                    error=e,
                )
            )

        return service

    def _display_notification(self, result_type, message, close=False):
        """Return the client action displaying the result of a test."""
        params = {"message": message, "type": result_type, "sticky": False}
        action = {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "context": dict(self.env.context, active_ids=self.ids),
            "params": params,
        }
        if close:
            action["target"] = "new"
            params["next"] = {"type": "ir.actions.act_window_close"}
        return action

    def _test_sftp_connection(self):
        """Open and close an SFTP connection. Return the result message."""
        sftp, transport = self.establish_sftp_connection()
        # Close SFTP Connection
        sftp.close()
        transport.close()
        return _("The SFTP connection was successful.")

    def _test_drive_connection(self):
        """Request the Google Drive account details. Return the result message."""
        service = self.provider_authenticate()
        about = service.about().get(fields="user, storageQuota").execute()
        return "%s\n%s" % (
            _("Google Drive Connection Successful!"),
            _("User: %s", about["user"]["displayName"]),
        )

    def _test_dropbox_connection(self):
        """Request the Dropbox account details. Return the result message."""
        dbx = self.provider_authenticate()
        account_info = dbx.users_get_current_account()
        return "%s\n%s" % (
            _("Dropbox Connection Successful!"),
            _("Account: %s...", account_info.account_id[:8]),
        )

    def test_connection(self):
        """Test the connection to the backup provider and display the result.

        Returns:
            dict: Action to display notification with the test result.
        """
        self.ensure_one()
        self.check_company_policy()

        test_methods = {
            "sftp": self._test_sftp_connection,
            "drive": self._test_drive_connection,
            "dropbox": self._test_dropbox_connection,
        }
        test_method = test_methods.get(self.backup_type)
        if not test_method:
            return self._display_notification(
                "warning",
                _(
                    "There was an internal issue. You Can not test connection for a "
                    "backup type different of SFTP, G.Drive or Dropbox."
                ),
                close=True,
            )

        try:
            result_msg = test_method()
            result_type = "success"
            _logger.info(result_msg)
        except AuthenticationException as auth_error:
            # Set a warning notification values due to an authentication error
            result_type = "warning"
            result_msg = _(
                "The SFTP connection failed due to authentication error.\nError: %s",
                auth_error,
            )
            _logger.error(result_msg)
        except Exception as e:
            # Set a danger notification values due to a failed test
            result_type = "danger"
            result_msg = _("The connection failed. Error: %s", e)
            _logger.error(result_msg)

        # Update State if the confirmation flag is true
        if result_type == "success" and self.env.context.get("confirm", False):
            self.confirm()

        # Display the test result
        return self._display_notification(result_type, result_msg, close=True)

    def _send_test_file_local(self, file_content, f_path, f_name):
        """Write the test file on the local file system. Return the result message."""
        # Try to create the file path if it does not exist
        os.makedirs(f_path, exist_ok=True)
        with open(f_path + f_name, "w") as file:
            file.write(file_content)
        return _("The Local file transference was successful.")

    def _send_test_file_sftp(self, file_content, f_path, f_name):
        """Send the test file through SFTP. Return the result message."""
        sftp, transport = self.establish_sftp_connection()
        try:
            # Upload the file to the server
            with sftp.file(f_path + f_name, "w") as f:
                f.write(file_content)
        finally:
            # Close SFTP Connection
            sftp.close()
            transport.close()
        return _("The SFTP file transference was successful.")

    def _send_test_file_drive(self, file_content, f_path, f_name):
        """Send the test file to Google Drive. Return the result message."""
        if not self.parent_folder:
            raise UserError(
                _(
                    "Error: Parent Folder ID not found. Please provide a valid parent "
                    "folder ID."
                )
            )
        # Create a file-like object from the byte content
        media = self.get_drive_file_media(
            io.BytesIO(file_content.encode("utf-8")), "application/txt"
        )
        # Get the current UTC time in ISO format
        current_time = datetime.now(timezone.utc).isoformat()

        # Fill file metadata
        file_metadata = {
            "name": f_name,
            "parents": [self.parent_folder],
            "description": "Uploaded from Odoo",
            "mimeType": "application/txt",
            "createdTime": current_time,
            "modifiedTime": current_time,
        }
        # Get Google Drive Service
        service = self.provider_authenticate()
        # Create the file
        file = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return "%s\n%s" % (
            _("The Google Drive test file transference was successful."),
            _("The file ID is: %s", file["id"]),
        )

    def _send_test_file_dropbox(self, file_content, f_path, f_name):
        """Send the test file to Dropbox. Return the result message."""
        # Get a Dropbox client
        dbx = self.provider_authenticate()
        # Upload the file content to Dropbox
        file = dbx.files_upload(file_content.encode("utf-8"), f_path + f_name)
        return "%s\n%s" % (
            _("The Dropbox test file transference was successful."),
            _("The file ID is: %s", file.id),
        )

    def send_test_file(self):
        """
        Sends a test file to the backup destination and displays the result.

        Returns:
            dict: Action to display notification with the test result.
        """
        self.ensure_one()
        self.check_company_policy()

        send_methods = {
            "local": self._send_test_file_local,
            "sftp": self._send_test_file_sftp,
            "drive": self._send_test_file_drive,
            "dropbox": self._send_test_file_dropbox,
        }
        send_method = send_methods.get(self.backup_type)
        if not send_method:
            # Display just on a strange case
            return self._display_notification(
                "warning",
                _(
                    "There was an internal issue. Please contact the system "
                    "administrator for assistance."
                ),
            )

        # Define the file path and content
        file_content = (
            "This is a test file created by eqp_backups module just for testing "
            "purposes"
        )
        f_path, f_name = self.get_file_path_details("test", "txt")

        try:
            result_msg = send_method(file_content, f_path, f_name)
            result_type = "success"
            _logger.info(result_msg)
        except AuthenticationException as auth_error:
            # Set a warning notification values due to an authentication error
            result_type = "warning"
            result_msg = _(
                "The SFTP transfer test failed due to authentication error.\n"
                "Error: %s",
                auth_error,
            )
            _logger.error(result_msg)
        except Exception as e:
            result_type = "danger"
            result_msg = _("Failed to create and send test file.\nError: %s", e)
            _logger.error(result_msg)

        # Provide feedback
        return self._display_notification(result_type, result_msg)

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
import re
import odoo
import glob
import json
import shutil
import logging
import tempfile
import subprocess

from odoo import api, models, fields, _
from odoo.service import db
from odoo.exceptions import UserError, ValidationError
from datetime import datetime, timezone

from odoo.tools import config, osutil
from odoo.tools.misc import exec_pg_environ, find_pg_tool

_logger = logging.getLogger(__name__)


# Overwriting the "db functions" to prevent that list_db=False will block the process


def dump_db_manifest(cr):
    pg_version = "%d.%d" % divmod(cr._obj.connection.server_version / 100, 100)
    cr.execute(
        "SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'"
    )
    modules = dict(cr.fetchall())
    manifest = {
        "odoo_dump": "1",
        "db_name": cr.dbname,
        "version": odoo.release.version,
        "version_info": odoo.release.version_info,
        "major_version": odoo.release.major_version,
        "pg_version": pg_version,
        "modules": modules,
    }
    return manifest


def _dump_filestore(db_name, dump_dir):
    """Copy the filestore to the given dump directory."""
    filestore = config.filestore(db_name)
    if os.path.exists(filestore):
        shutil.copytree(filestore, os.path.join(dump_dir, "filestore"))


def _dump_database(db_name, dump_dir, backup_format):
    """Dump the database to the given dump directory."""
    cmd = [find_pg_tool("pg_dump"), "--no-owner", db_name]
    env = exec_pg_environ()

    if backup_format == "zip":
        dump_file = os.path.join(dump_dir, "dump.sql")
        cmd.insert(-1, f"--file={dump_file}")

        with open(os.path.join(dump_dir, "manifest.json"), "w") as fh:
            db = odoo.sql_db.db_connect(db_name)
            with db.cursor() as cr:
                json.dump(dump_db_manifest(cr), fh, indent=4)

        subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            check=True,
        )

    else:
        cmd.insert(-1, "--format=c")
        return subprocess.Popen(
            cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE
        ).stdout


def _create_zip(dump_dir, stream=None):
    """Create a ZIP archive of the given directory and write it to the stream or return a temp file."""
    if not stream:
        stream = tempfile.TemporaryFile()

    osutil.zip_dir(
        dump_dir,
        stream,
        include_dir=False,
        fnct_sort=lambda file_name: file_name != "dump.sql",
    )
    stream.seek(0)
    return stream


def dump_filestore(db_name, stream=None, backup_format="zip"):
    """Dump the database Filestore into a file-like object `stream`."""
    _logger.info("Backing up Filestore: %s (format: %s)", db_name, backup_format)

    with tempfile.TemporaryDirectory() as dump_dir:
        _dump_filestore(db_name, dump_dir)
        return _create_zip(dump_dir, stream)


def dump_db(db_name, stream=None, backup_format="zip"):
    """Dump the database into a file-like object `stream`."""
    _logger.info("Backing up DB: %s (format: %s)", db_name, backup_format)

    if backup_format == "zip":
        with tempfile.TemporaryDirectory() as dump_dir:
            _dump_database(db_name, dump_dir, backup_format)
            return _create_zip(dump_dir, stream)
    else:
        return _dump_database(db_name, None, backup_format)


def dump_db_full(db_name, stream=None, backup_format="zip"):
    """Dump both the database and Filestore into a file-like object `stream`."""
    _logger.info("Backing up DB & Filestore: %s (format: %s)", db_name, backup_format)

    if backup_format == "zip":
        with tempfile.TemporaryDirectory() as dump_dir:
            _dump_filestore(db_name, dump_dir)
            _dump_database(db_name, dump_dir, backup_format)
            return _create_zip(dump_dir, stream)
    else:
        return _dump_database(db_name, None, backup_format)


class BackupRecord(models.Model):
    """Model to manage EQP Automatic Backup Records."""

    _name = "backup.record"
    _description = "EQP Automatic Backup Records"
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
    user_id = fields.Many2one(
        "res.users",
        string="Responsible user",
        index=True,
        copy=False,
        required=True,
        ondelete="cascade",
        default=lambda self: self.env.user,
        tracking=True,
        help="Assign a responsible user for this Backup Record.",
    )

    name = fields.Char(string="Name", required=True, tracking=True, help="Record Name")
    description = fields.Text(
        string="Description", copy=False, help="Add a brief description of the record"
    )

    db_name = fields.Char(
        string="Database Name", required=True, tracking=True, help="Data Base name"
    )
    db_names = fields.Text(string="Available Databases", copy=False, readonly=True)
    frequency = fields.Selection(
        [
            ("years", "Yearly"),
            ("months", "Monthly"),
            ("weeks", "Weekly"),
            ("days", "Daily"),
            ("hours", "Hourly"),
        ],
        string="Backup Frequency",
        required=True,
        tracking=True,
        help="Select the frequency at which you would like your backups to be generated.",
    )
    type = fields.Selection(
        [("full", "Full (DB & FS)"), ("db", "Data Base"), ("fs", "File Store")],
        string="Type",
        default="full",
        required=True,
        tracking=True,
        help="Select the type of backup to execute:\n"
        "- Full (DB & FS): Backs up both the database and file store.\n"
        "- Database Only: Backs up only the database.\n"
        "- File Store Only: Backs up only the file storage (attachments and other media).",
    )
    backup_lifespan_qty = fields.Integer(
        string="Backup Lifespan qty",
        required=True,
        tracking=True,
        default=-1,
        help="When the number of backups exceeds this limit, the system will "
        "automatically delete the older backups, ensuring that only the "
        "specified number of backups are retained.\n"
        "To DISABLE this functionality put '-1'.",
    )
    success_mail_send = fields.Boolean(
        string="Email when Success",
        copy=False,
        tracking=True,
        help="Send an email when the backup process is executed Successfully.",
    )
    failure_mail_send = fields.Boolean(
        string="Email when Failure",
        copy=False,
        tracking=True,
        help="Send an email when the backup process present a Failure .",
    )
    success_mail_policy = fields.Boolean(
        related="company_id.eqp_backups_enable_success_email"
    )
    failure_mail_policy = fields.Boolean(
        related="company_id.eqp_backups_enable_failure_email"
    )

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("confirmed", "Confirmed"),
            ("paused", "Paused"),
            ("archived", "Archived"),
        ],
        string="Status",
        default="draft",
        readonly=True,
        tracking=True,
        copy=False,
        help="Backup Record Status (If the status is 'Paused,' it indicates that the server is "
        "in a state other than 'confirmed.)",
    )
    server_id = fields.Many2one(
        "backup.server",
        string="Server",
        required=True,
        index=True,
        tracking=True,
        ondelete="restrict",
        help="Select a Backup server (Only Confirmed Servers can be used).",
    )
    server_type = fields.Selection(
        related="server_id.backup_type", string="Server Type"
    )
    chunk_size = fields.Integer(
        string="Chunk Size (MB)",
        required=True,
        tracking=True,
        default=10,
        help="As the backup size could be very large we have to divide it in chunks: "
        "A reference basic guideline:\n\n"
        "Smaller files: For smaller files (up to a few hundred megabytes), a chunk size of 1MB to 5MB can be suitable.\n"
        "Medium-sized files: For files ranging from a few hundred megabytes to a few gigabytes, a chunk size of 5MB to 10MB is often appropriate.\n"
        "Large files: For very large files (several gigabytes or more), you may still want to use smaller chunk sizes (e.g., 5MB to 10MB) to ensure smoother handling and better resilience to network issues.\n"
        "Note: If set to 0 ('zero'), the system will use the default 'files_upload' method (without chunks).",
    )

    cron_id = fields.Many2one(
        "ir.cron",
        string="Scheduled Action",
        readonly=True,
        tracking=True,
        ondelete="restrict",
        copy=False,
        help="Scheduled cron which ensures the automatic execution of backups at "
        "specific times.",
    )

    last_execution_result = fields.Text(
        string="Last Execution Result",
        readonly=True,
        help="Access the details of the most recent execution here.",
    )

    _name_unique = models.Constraint(
        "unique(name, company_id)", "A unique name per company."
    )
    _unique_cron_id = models.Constraint(
        "UNIQUE(cron_id)", "One Cron should only have one BackUp record."
    )
    _check_backup_lifespan_qty = models.Constraint(
        "CHECK(backup_lifespan_qty = -1 OR backup_lifespan_qty > 1)",
        "The Backup span qty must be either -1 or greater than 1",
    )

    # Constraints
    @api.constrains("chunk_size")
    def _check_chunk_size(self):
        for record in self:
            if record.chunk_size < 0:
                raise ValidationError(
                    _("The Chunk Size must be zero or a positive number.")
                )

    # Static Methods
    @staticmethod
    def _generate_backup(db_name, file, extension, bu_type):
        # Determine backup type and generate backup
        if bu_type == "fs":
            bu_file = dump_filestore(db_name, file, extension)
        elif bu_type == "db":
            bu_file = dump_db(db_name, file, extension)
        else:
            bu_file = dump_db_full(db_name, file, extension)
        return bu_file

    def update_cron_state(self, state):
        """Update the state of the associated cron job.

        Args:
            state (bool): New state for the cron job (active/inactive).
        """
        for record in self:
            if record.cron_id:
                # The cron is fully managed by this model, so backup users do not
                # need (and must not be granted) direct access to `ir.cron`.
                record.cron_id.sudo().write({"active": state})

    def write(self, vals):
        """Override write method to handle state updates.

        Args:
            vals (dict): Values to be written.

        Returns:
            dict: Written values.
        """
        if "active" in vals:
            if vals["active"]:
                vals = dict(vals, state="draft")
            else:
                vals = dict(vals, state="archived")
                self.update_cron_state(False)
        return super(BackupRecord, self).write(vals)

    def copy(self, default=None):
        """Create a copy of the backup record.

        Args:
            default (dict): Default values for the new record.

        Returns:
            models.Model: New backup record.
        """
        self.ensure_one()
        chosen_name = default.get("name") if default else ""
        new_name = chosen_name or _("%s (copy)", self.name)
        default = dict(default or {}, name=new_name)
        return super(BackupRecord, self).copy(default)

    def check_valid_state(self, just_server=False):
        """Check if the backup record is in a valid state for certain operations.

        Args:
            just_server (bool): If True, only check the state of the associated server.

        Raises:
            UserError: If the state is not valid.
        """
        for record in self:
            if not just_server and record.state != "confirmed":
                raise UserError(
                    _(
                        "This method can only be executed on a Backup record in the "
                        '"confirmed" state.'
                    )
                )
            if record.server_id.state != "confirmed":
                raise UserError(
                    _(
                        "This method can only be executed with a Backup Server in the "
                        '"confirmed" state.'
                    )
                )

    def revert_state(self):
        """Revert the state of the backup record to 'draft'.

        If the record is in the 'confirmed' state, it will be reverted to 'draft',
         and the associated cron job state will be updated if applicable.
        """
        for record in self:
            record.update_cron_state(False)
            if record.state == "confirmed":
                record.write({"state": "draft", "db_names": ""})

    def validate_db(self):
        """Validate database credentials and confirm backup record.

        Returns:
            dict: Action to display notification with validation result.
        """
        self.ensure_one()
        dbs = db.list_dbs(force=True)

        # Set a Success values by default.
        result_type = "success"
        result_msg = _("Database validation executed Successfully.")

        db_name = self.db_name
        # Validates if the database exists
        if db_name not in dbs:
            # Set a warning notification values by default (Display just on a strange case).
            result_type = "warning"
            result_msg = _(
                'The specified Database Name "%s" does not exist. Please verify and '
                "try again.",
                db_name,
            )

        if result_type == "success" and self.env.context.get("confirm", False):

            # Validate confirmed state
            self.check_valid_state(True)
            # Create/update Cron
            vals = {"state": "confirmed"}

            # Define the Cron values
            model_id = self.env["ir.model"]._get_id("backup.record")
            cron_values = {
                "active": True,
                "name": f"{self.name} - Cron",
                "model_id": model_id,
                "state": "code",
                "code": f"model._scheduled_backup_process(record_id={self.id})",
                "priority": 3,
            }

            # The cron values are fully controlled here (no user input ends up in
            # the executed code), so it is created/updated with elevated privileges.
            if self.cron_id:
                # Re-Write Existing Cron
                self.cron_id.sudo().write(cron_values)
            else:
                # Default interval_number and interval_type on the cron then the user can modify it if required.
                cron_values["interval_number"] = 1 if self.frequency != "years" else 12
                cron_values["interval_type"] = (
                    self.frequency if self.frequency != "years" else "months"
                )
                cron_values["user_id"] = self.env.uid
                # Create new Cron
                cron_job = self.env["ir.cron"].sudo().create(cron_values)
                # Reference the cron with the Backup record
                vals["cron_id"] = cron_job.id

            # Update State if the confirmation flag is true
            self.write(vals)

        # Display the test result
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "context": dict(self.env.context, active_ids=self.ids),
            "target": "new",
            "params": {
                "message": result_msg,
                "type": result_type,
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def show_databases(self):
        """Update the 'db_names' field with the available databases."""
        dbs = db.list_dbs(force=True)
        dbs_txt = "\n".join(name for name in dbs)
        self.write({"db_names": dbs_txt})

    def notify_result(self, result_type):
        """Notify the result of the backup process.

        Args:
            result_type (str): Type of result (success/danger).
        """
        for record in self:
            template = None

            if (
                result_type == "success"
                and record.success_mail_send
                and record.success_mail_policy
            ):
                template = self.env.ref(
                    "eqp_backups.email_template_data_automatic_backup_success"
                )
            elif (
                result_type == "danger"
                and record.failure_mail_send
                and record.failure_mail_policy
            ):
                template = self.env.ref(
                    "eqp_backups.email_template_data_automatic_backup_failed"
                )
            if template:
                template.send_mail(record.id, force_send=True)

    # Backup helpers
    def _get_backup_file_pattern(self):
        """Return the compiled pattern matching the backup files of this record's
        database, as named by `backup.server.get_file_path_details`.

        The retention process relies on it to never delete files which are not
        backups of the same database (other databases or unrelated files sharing
        the same destination).
        """
        self.ensure_one()
        return re.compile(
            r"^Backup_%s_\d{4}-\d{2}-\d{2}_\d{2}\.\d{2}\.\d{2}\.zip$"
            % re.escape(self.db_name)
        )

    def _get_expired_backups(self, backups, current):
        """Return the backups exceeding the lifespan quantity.

        Args:
            backups (list): Backup identifiers sorted from the latest to the oldest.
            current: Identifier of the backup which has just been generated.

        Returns:
            list: Identifiers of the backups to delete.
        """
        self.ensure_one()
        backups_to_keep = self.backup_lifespan_qty
        if backups_to_keep <= 0:
            return []
        # The new backup always counts, even when the provider does not list it yet
        if current not in backups:
            backups_to_keep -= 1
        return [backup for backup in backups[backups_to_keep:] if backup != current]

    def _backup_to_local(self, server, destination_path, file_name):
        """Generate the backup on the local file system and return the result message."""
        self.ensure_one()
        file_path = destination_path + file_name
        # Try to create the destination path if it does not exist
        os.makedirs(destination_path, exist_ok=True)
        try:
            with open(file_path, "wb") as file:
                self._generate_backup(self.db_name, file, "zip", self.type)
        except Exception:
            # Do not leave a truncated backup behind
            if os.path.exists(file_path):
                os.remove(file_path)
            raise

        # Process which deletes old backups
        if self.backup_lifespan_qty > 0:
            pattern = self._get_backup_file_pattern()
            backup_files = [
                bu_file
                for bu_file in glob.glob(os.path.join(destination_path, "Backup_*.zip"))
                if pattern.match(os.path.basename(bu_file))
            ]
            # Sort backup files based on modification time (latest first)
            backup_files.sort(key=os.path.getmtime, reverse=True)
            for old_backup in self._get_expired_backups(backup_files, file_path):
                os.remove(old_backup)

        return _("Local Backup process executed successfully.")

    def _backup_to_sftp(self, server, destination_path, file_name):
        """Generate the backup and send it through SFTP. Return the result message."""
        self.ensure_one()
        sftp, transport = server.establish_sftp_connection()
        try:
            with self._generate_backup(
                self.db_name, None, "zip", self.type
            ) as bu_file_obj:
                # Upload the Backup file object to the remote folder
                sftp.putfo(bu_file_obj, destination_path + file_name)

            # Process which deletes old backups
            if self.backup_lifespan_qty > 0:
                pattern = self._get_backup_file_pattern()
                backup_files = [
                    bu_file
                    for bu_file in sftp.listdir(destination_path)
                    if pattern.match(bu_file)
                ]
                # Sort backup files based on modification time (latest first)
                backup_files.sort(
                    key=lambda bu_file: sftp.stat(destination_path + bu_file).st_mtime,
                    reverse=True,
                )
                for old_backup in self._get_expired_backups(backup_files, file_name):
                    sftp.remove(destination_path + old_backup)
        finally:
            sftp.close()
            transport.close()

        return _("SFTP Backup transference process executed successfully.")

    def _backup_to_drive(self, server, destination_path, file_name):
        """Generate the backup and send it to Google Drive. Return the result message."""
        self.ensure_one()
        # Get Google Drive Service
        service = server.provider_authenticate()
        # Get the current UTC time in ISO format
        current_time = datetime.now(timezone.utc).isoformat()
        file_metadata = {
            "name": file_name,
            "parents": [server.parent_folder],
            "description": "Uploaded from Odoo",
            "mimeType": "application/zip",
            "createdTime": current_time,
            "modifiedTime": current_time,
        }
        with self._generate_backup(self.db_name, None, "zip", self.type) as bu_file_obj:
            # Create a MediaIoBaseUpload object from the backup file object
            media = server.get_drive_file_media(bu_file_obj, "application/zip")
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

        # Process which deletes old backups
        if self.backup_lifespan_qty > 0:
            parent_folder = server.parent_folder.replace("\\", "\\\\").replace(
                "'", "\\'"
            )
            # Retrieve a list of all backup files in the parent folder on Google Drive
            results = (
                service.files()
                .list(
                    q=f"'{parent_folder}' in parents and "
                    "mimeType='application/zip' and trashed=false",
                    fields="files(id, name, createdTime)",
                    pageSize=1000,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            pattern = self._get_backup_file_pattern()
            backup_files = [
                bu_file
                for bu_file in results.get("files", [])
                if pattern.match(bu_file.get("name") or "")
            ]
            # Sort backup files based on creation time (latest first)
            backup_files.sort(key=lambda bu_file: bu_file["createdTime"], reverse=True)
            backup_ids = [bu_file["id"] for bu_file in backup_files]
            for old_backup_id in self._get_expired_backups(backup_ids, file["id"]):
                service.files().delete(
                    fileId=old_backup_id, supportsAllDrives=True
                ).execute()

        return "%s\n%s" % (
            _("Google Drive Backup process executed successfully."),
            _("The file ID is: %s", file["id"]),
        )

    def _dropbox_upload(self, dbx, bu_file_obj, file_path):
        """Upload the backup file object to Dropbox, in chunks when a chunk size is set.

        Returns:
            dropbox.files.FileMetadata: Metadata of the uploaded file.
        """
        self.ensure_one()
        chunk_size = self.chunk_size * 1024 * 1024  # MB chunk size
        if not chunk_size:
            # Upload the entire file in one request if no chunking is specified
            return dbx.files_upload(bu_file_obj.read(), file_path)

        import dropbox

        chunk = bu_file_obj.read(chunk_size)
        session = dbx.files_upload_session_start(chunk)
        cursor = dropbox.files.UploadSessionCursor(
            session_id=session.session_id, offset=len(chunk)
        )
        commit = dropbox.files.CommitInfo(path=file_path)
        while True:
            chunk = bu_file_obj.read(chunk_size)
            if not chunk:
                return dbx.files_upload_session_finish(chunk, cursor, commit)
            dbx.files_upload_session_append_v2(chunk, cursor)
            cursor.offset += len(chunk)

    def _backup_to_dropbox(self, server, destination_path, file_name):
        """Generate the backup and send it to Dropbox. Return the result message."""
        self.ensure_one()
        # Get Dropbox Client
        dbx = server.provider_authenticate()
        with self._generate_backup(self.db_name, None, "zip", self.type) as bu_file_obj:
            file = self._dropbox_upload(dbx, bu_file_obj, destination_path + file_name)

        # Process which deletes old backups
        if self.backup_lifespan_qty > 0:
            pattern = self._get_backup_file_pattern()
            # Retrieve the backup files of the Dropbox folder ("" is the root folder)
            results = dbx.files_list_folder(path=destination_path.rstrip("/"))
            backup_files = [
                bu_file for bu_file in results.entries if pattern.match(bu_file.name)
            ]
            # Sort backup files based on server_modified timestamp (latest first)
            backup_files.sort(
                key=lambda bu_file: bu_file.server_modified.timestamp(),
                reverse=True,
            )
            paths_by_id = {bu_file.id: bu_file.path_display for bu_file in backup_files}
            for old_backup_id in self._get_expired_backups(list(paths_by_id), file.id):
                dbx.files_delete_v2(path=paths_by_id[old_backup_id])

        return "%s\n%s" % (
            _("Dropbox Backup process executed successfully."),
            _("The file ID is: %s", file.id),
        )

    def _execute_backup(self):
        """Generate the backup and send it to the destination of the server.

        Returns:
            str: Message describing the successful execution.

        Raises:
            UserError: If the database or the backup type are not valid.
        """
        self.ensure_one()
        # Credentials are restricted to the backup admins, while the backups can be
        # executed by any backup user (or by the cron on their behalf).
        server = self.server_id.sudo()
        db_name = self.db_name

        # The database name ends up in a `pg_dump` call and in the filestore path:
        # only accept the name of an existing database.
        if db_name not in db.list_dbs(force=True):
            raise UserError(
                _(
                    'The specified Database Name "%s" does not exist. Please verify '
                    "and try again.",
                    db_name,
                )
            )

        backup_methods = {
            "local": self._backup_to_local,
            "sftp": self._backup_to_sftp,
            "drive": self._backup_to_drive,
            "dropbox": self._backup_to_dropbox,
        }
        backup_method = backup_methods.get(server.backup_type)
        if not backup_method:
            raise UserError(
                _(
                    "Failed to execute the automatic backup process for record ID %s "
                    "due to an unrecognized backup type.",
                    self.id,
                )
            )

        # Get file path details
        destination_path, file_name = server.get_file_path_details(db_name, "zip")
        return backup_method(server, destination_path, file_name)

    def _scheduled_backup_process(self, record_id):
        """Perform the scheduled backup process.

        Args:
            record_id (int): ID of the backup record.

        Returns:
            bool: True if the process was successful, False otherwise.
        """
        record = self.browse(record_id)

        # Validate confirmed state
        record.check_valid_state()

        try:
            result_msg = record._execute_backup()
            result_type = "success"
            _logger.info(result_msg)
        except Exception as e:
            result_type = "danger"
            result_msg = _(
                "Failed to create and send backup file.\nError: %s", str(e) or repr(e)
            )
            _logger.exception(
                "Backup process failed for backup record ID %s", record.id
            )

        # Display an alert window for manual execution, or alternatively, send an email if applicable.
        if not self.env.context.get("manual_execution", False):
            time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            record.last_execution_result = (
                f"RESULT TYPE: {result_type}\nDATE (yyyy-mm-dd hh:mm:ss): {time}\n"
                f"DETAILS: {result_msg}"
            )
            record.notify_result(result_type)
            return True
        else:
            return result_type, result_msg

    def manual_execution(self):
        """Execute backup manually.

        Returns:
            dict: Action to display notification with the execution result.
        """
        self.ensure_one()
        self.check_valid_state()
        result_type, msg = self.with_context(
            manual_execution=True
        )._scheduled_backup_process(self.id)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "context": dict(self.env.context, active_ids=self.ids),
            "params": {
                "message": msg,
                "type": result_type,
                "sticky": False,
            },
        }

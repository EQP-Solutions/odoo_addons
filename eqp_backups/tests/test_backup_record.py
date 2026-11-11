# -*- coding: utf-8 -*-
###########################################################################################
#
#    EQP Solutions
#
#    Copyright (C) 2023-TODAY EQP Solutions (<https://www.eqpsolutions.com>)
#    Author: EQP Solutions (<info@eqpsolutions.com>)
#
#    This program is free software: you can redistribute it and/or modify it under
#    the terms of the GNU Lesser General Public License (LGPL) as published by the
#    Free Software Foundation, version 3 of the License. See the LICENSE file.
#
###########################################################################################
"""
Tests for backup.record model.

TestBackupRecord:
  - test_name_unique_per_company_raises_integrity_error
  - test_backup_lifespan_qty_one_raises_integrity_error
  - test_chunk_size_negative_raises_validation_error
  - test_copy_appends_suffix_and_resets_state
  - test_validate_db_unknown_database_returns_warning
  - test_validate_db_without_confirm_keeps_draft_state
  - test_validate_db_confirm_creates_cron
  - test_validate_db_confirm_yearly_frequency_uses_twelve_months
  - test_validate_db_confirm_server_not_confirmed_raises_user_error
  - test_validate_db_confirm_as_backup_user_creates_cron
  - test_revert_state_deactivates_cron
  - test_archive_deactivates_cron_and_sets_archived_state
  - test_scheduled_backup_record_not_confirmed_raises_user_error
  - test_scheduled_backup_local_creates_file_and_stores_result
  - test_scheduled_backup_from_cron_model_uses_record_type
  - test_scheduled_backup_local_retention_keeps_other_files
  - test_scheduled_backup_unknown_database_reports_failure
  - test_scheduled_backup_failure_removes_partial_file_and_sends_mail
  - test_scheduled_backup_success_sends_mail_when_enabled
  - test_scheduled_backup_sftp_uploads_and_prunes_old_backups
  - test_scheduled_backup_sftp_failure_closes_connection
  - test_scheduled_backup_drive_prunes_only_own_backups
  - test_scheduled_backup_dropbox_single_request_prunes_old_backups
  - test_scheduled_backup_dropbox_chunked_upload_uses_session
  - test_manual_execution_returns_notification_without_storing_result
  - test_manual_execution_as_backup_user_succeeds
"""

import io
import os
import sys

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from psycopg2 import IntegrityError

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import BACKUP_CONTENT, LIST_DBS, EqpBackupsCommon

RECORD_LOGGER = "odoo.addons.eqp_backups.models.backup_record"


@tagged("post_install", "-at_install")
class TestBackupRecord(EqpBackupsCommon):
    """Backup jobs: validation, cron handling, execution and retention."""

    def _run_scheduled(self, record=None):
        """Run the backup the same way the cron does (on the empty model)."""
        record = record or self.record
        return self.BackupRecord._scheduled_backup_process(record_id=record.id)

    def _create_record(self, server, **values):
        """Create and confirm a backup record on the given server."""
        record = self.BackupRecord.create(
            {
                "name": f"Record on {server.name}",
                "db_name": self.db_name,
                "frequency": "days",
                "server_id": server.id,
                "backup_lifespan_qty": 2,
                **values,
            }
        )
        self._confirm_record(record)
        return record

    # Constraints
    def test_name_unique_per_company_raises_integrity_error(self):
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self.record.copy({"name": self.record.name})

    def test_backup_lifespan_qty_one_raises_integrity_error(self):
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self.record.backup_lifespan_qty = 1
            self.env.flush_all()

    def test_chunk_size_negative_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            self.record.chunk_size = -1

    def test_copy_appends_suffix_and_resets_state(self):
        self._confirm_record()
        new_record = self.record.copy()
        self.assertEqual(new_record.name, f"{self.record.name} (copy)")
        self.assertEqual(new_record.state, "draft")
        self.assertFalse(new_record.cron_id)

    # Validation & cron
    def test_validate_db_unknown_database_returns_warning(self):
        with patch(LIST_DBS, return_value=["another_db"]):
            action = self.record.with_context(confirm=True).validate_db()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertEqual(self.record.state, "draft")
        self.assertFalse(self.record.cron_id)

    def test_validate_db_without_confirm_keeps_draft_state(self):
        with patch(LIST_DBS, return_value=[self.db_name]):
            action = self.record.validate_db()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(self.record.state, "draft")

    def test_validate_db_confirm_creates_cron(self):
        action = self._confirm_record()
        cron = self.record.cron_id
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(self.record.state, "confirmed")
        self.assertTrue(cron.active)
        self.assertEqual(
            cron.code,
            f"model._scheduled_backup_process(record_id={self.record.id})",
        )
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "days"))

    def test_validate_db_confirm_yearly_frequency_uses_twelve_months(self):
        self.record.frequency = "years"
        self._confirm_record()
        cron = self.record.cron_id
        self.assertEqual((cron.interval_number, cron.interval_type), (12, "months"))

    def test_validate_db_confirm_server_not_confirmed_raises_user_error(self):
        self.server.revert_state()
        with self.assertRaises(UserError):
            self._confirm_record()

    def test_validate_db_confirm_as_backup_user_creates_cron(self):
        record = self.record.with_user(self.backup_user)
        self._confirm_record(record)
        self.assertEqual(record.state, "confirmed")
        cron = record.cron_id.sudo()
        self.assertTrue(cron.active)
        self.assertEqual(cron.user_id, self.backup_user)

    def test_revert_state_deactivates_cron(self):
        self._confirm_record()
        self.record.revert_state()
        self.assertEqual(self.record.state, "draft")
        self.assertFalse(self.record.cron_id.active)

    def test_archive_deactivates_cron_and_sets_archived_state(self):
        self._confirm_record()
        self.record.action_archive()
        self.assertEqual(self.record.state, "archived")
        self.assertFalse(self.record.cron_id.active)

    # Execution: local
    def test_scheduled_backup_record_not_confirmed_raises_user_error(self):
        with self._mock_backup(), self.assertRaises(UserError):
            self._run_scheduled()

    def test_scheduled_backup_local_creates_file_and_stores_result(self):
        tmp_dir = self._use_temp_destination()
        self._confirm_record()
        with self._mock_backup():
            self.assertTrue(self._run_scheduled())
        files = os.listdir(tmp_dir)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], rf"^Backup_{self.db_name}_.*\.zip$")
        with open(os.path.join(tmp_dir, files[0]), "rb") as backup:
            self.assertEqual(backup.read(), BACKUP_CONTENT)
        self.assertIn("RESULT TYPE: success", self.record.last_execution_result)

    def test_scheduled_backup_from_cron_model_uses_record_type(self):
        self._use_temp_destination()
        self.record.type = "db"
        self._confirm_record()
        with self._mock_backup() as generate_backup:
            self._run_scheduled()
        generate_backup.assert_called_once()
        self.assertEqual(generate_backup.call_args.args[0], self.db_name)
        self.assertEqual(generate_backup.call_args.args[3], "db")

    def test_scheduled_backup_local_retention_keeps_other_files(self):
        tmp_dir = self._use_temp_destination()
        self._confirm_record()
        own_backups = [
            self._backup_name("2020-01-01"),
            self._backup_name("2020-01-02"),
            self._backup_name("2020-01-03"),
        ]
        other_files = [
            f"Backup_{self.db_name}_other_2020-01-01_00.00.00.zip",
            "Backup_notes.zip",
            "unrelated.zip",
        ]
        for index, name in enumerate(own_backups + other_files):
            path = os.path.join(tmp_dir, name)
            with open(path, "wb") as old_file:
                old_file.write(b"old")
            # Older than the new backup, own backups sorted by date
            os.utime(path, (1000 + index, 1000 + index))

        with self._mock_backup():
            self._run_scheduled()

        remaining = set(os.listdir(tmp_dir))
        new_backups = remaining - set(own_backups) - set(other_files)
        self.assertEqual(len(new_backups), 1)
        # Lifespan is 2: the new backup and the latest previous one are kept
        self.assertIn(own_backups[2], remaining)
        self.assertNotIn(own_backups[0], remaining)
        self.assertNotIn(own_backups[1], remaining)
        self.assertTrue(set(other_files) <= remaining)

    @mute_logger(RECORD_LOGGER)
    def test_scheduled_backup_unknown_database_reports_failure(self):
        tmp_dir = self._use_temp_destination()
        self._confirm_record()
        with self._mock_backup(dbs=["another_db"]) as generate_backup:
            self._run_scheduled()
        generate_backup.assert_not_called()
        self.assertEqual(os.listdir(tmp_dir), [])
        self.assertIn("RESULT TYPE: danger", self.record.last_execution_result)

    @mute_logger(RECORD_LOGGER)
    def test_scheduled_backup_failure_removes_partial_file_and_sends_mail(self):
        tmp_dir = self._use_temp_destination()
        self._confirm_record()
        self.record.failure_mail_send = True
        with self._mock_backup(side_effect=OSError("disk full")), patch.object(
            type(self.env["mail.template"]), "send_mail"
        ) as send_mail:
            self._run_scheduled()
        self.assertEqual(os.listdir(tmp_dir), [])
        self.assertIn("RESULT TYPE: danger", self.record.last_execution_result)
        self.assertIn("disk full", self.record.last_execution_result)
        send_mail.assert_called_once_with(self.record.id, force_send=True)

    def test_scheduled_backup_success_sends_mail_when_enabled(self):
        self._use_temp_destination()
        self._confirm_record()
        with self._mock_backup(), patch.object(
            type(self.env["mail.template"]), "send_mail"
        ) as send_mail:
            self._run_scheduled()
            send_mail.assert_not_called()
            self.record.success_mail_send = True
            self._run_scheduled()
            send_mail.assert_called_once_with(self.record.id, force_send=True)

    # Execution: SFTP
    def test_scheduled_backup_sftp_uploads_and_prunes_old_backups(self):
        server = self._create_server("sftp")
        record = self._create_record(server)
        mtimes = {
            self._backup_name("2020-01-01"): 100,
            self._backup_name("2020-01-02"): 200,
            self._backup_name("2020-01-03"): 300,
            "Backup_otherdb_2020-01-01_00.00.00.zip": 50,
            "notes.zip": 10,
        }
        sftp, transport = MagicMock(), MagicMock()
        sftp.listdir.return_value = list(mtimes)
        sftp.stat.side_effect = lambda path: SimpleNamespace(
            st_mtime=mtimes[os.path.basename(path)]
        )
        with self._mock_backup(), patch.object(
            type(self.BackupServer),
            "establish_sftp_connection",
            return_value=(sftp, transport),
        ):
            self._run_scheduled(record)

        self.assertIn("RESULT TYPE: success", record.last_execution_result)
        uploaded_path = sftp.putfo.call_args.args[1]
        self.assertRegex(uploaded_path, rf"^/backups/Backup_{self.db_name}_.*\.zip$")
        # The new backup is not listed yet: it still counts in the lifespan (2)
        self.assertCountEqual(
            sftp.remove.call_args_list,
            [
                call("/backups/" + self._backup_name("2020-01-01")),
                call("/backups/" + self._backup_name("2020-01-02")),
            ],
        )
        sftp.close.assert_called_once()
        transport.close.assert_called_once()

    @mute_logger(RECORD_LOGGER)
    def test_scheduled_backup_sftp_failure_closes_connection(self):
        server = self._create_server("sftp")
        record = self._create_record(server)
        sftp, transport = MagicMock(), MagicMock()
        sftp.putfo.side_effect = OSError("connection lost")
        with self._mock_backup(), patch.object(
            type(self.BackupServer),
            "establish_sftp_connection",
            return_value=(sftp, transport),
        ):
            self._run_scheduled(record)
        self.assertIn("RESULT TYPE: danger", record.last_execution_result)
        self.assertIn("connection lost", record.last_execution_result)
        sftp.remove.assert_not_called()
        sftp.close.assert_called_once()
        transport.close.assert_called_once()

    # Execution: Google Drive
    def test_scheduled_backup_drive_prunes_only_own_backups(self):
        server = self._create_server("drive", parent_folder="folder'id")
        record = self._create_record(server)
        service = MagicMock()
        files_api = service.files.return_value
        files_api.create.return_value.execute.return_value = {"id": "new"}
        files_api.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": "new",
                    "name": self._backup_name("2020-01-04"),
                    "createdTime": "2020-01-04T00:00:00Z",
                },
                {
                    "id": "old-1",
                    "name": self._backup_name("2020-01-01"),
                    "createdTime": "2020-01-01T00:00:00Z",
                },
                {
                    "id": "old-2",
                    "name": self._backup_name("2020-01-02"),
                    "createdTime": "2020-01-02T00:00:00Z",
                },
                {
                    "id": "unrelated",
                    "name": "accounting-export.zip",
                    "createdTime": "2019-01-01T00:00:00Z",
                },
            ]
        }
        with self._mock_backup(), patch.object(
            type(self.BackupServer), "provider_authenticate", return_value=service
        ), patch.object(type(self.BackupServer), "get_drive_file_media"):
            self._run_scheduled(record)

        self.assertIn("RESULT TYPE: success", record.last_execution_result)
        self.assertIn("new", record.last_execution_result)
        create_kwargs = files_api.create.call_args.kwargs
        self.assertEqual(create_kwargs["body"]["parents"], ["folder'id"])
        self.assertTrue(create_kwargs["supportsAllDrives"])
        list_kwargs = files_api.list.call_args.kwargs
        self.assertIn("'folder\\'id' in parents", list_kwargs["q"])
        self.assertTrue(list_kwargs["includeItemsFromAllDrives"])
        # Lifespan is 2: only the oldest backup of this database is deleted
        files_api.delete.assert_called_once_with(fileId="old-1", supportsAllDrives=True)

    # Execution: Dropbox
    def _dropbox_entries(self):
        return [
            SimpleNamespace(
                id=f"id:{day}",
                name=self._backup_name(f"2020-01-0{day}"),
                path_display="/backups/" + self._backup_name(f"2020-01-0{day}"),
                server_modified=datetime(2020, 1, day),
            )
            for day in (1, 2, 3)
        ] + [
            # A sub-folder: no `server_modified` attribute
            SimpleNamespace(id="id:folder", name="Archive", path_display="/Archive"),
            SimpleNamespace(
                id="id:doc",
                name="contract.zip",
                path_display="/backups/contract.zip",
                server_modified=datetime(2019, 1, 1),
            ),
        ]

    def test_scheduled_backup_dropbox_single_request_prunes_old_backups(self):
        server = self._create_server("dropbox")
        record = self._create_record(server, chunk_size=0)
        dbx = MagicMock()
        dbx.files_upload.return_value = SimpleNamespace(id="id:3")
        dbx.files_list_folder.return_value = SimpleNamespace(
            entries=self._dropbox_entries()
        )
        with self._mock_backup(), patch.object(
            type(self.BackupServer), "provider_authenticate", return_value=dbx
        ):
            self._run_scheduled(record)

        self.assertIn("RESULT TYPE: success", record.last_execution_result)
        content, path = dbx.files_upload.call_args.args
        self.assertEqual(content, BACKUP_CONTENT)
        self.assertRegex(path, rf"^/backups/Backup_{self.db_name}_.*\.zip$")
        dbx.files_list_folder.assert_called_once_with(path="/backups")
        dbx.files_delete_v2.assert_called_once_with(
            path="/backups/" + self._backup_name("2020-01-01")
        )

    def test_scheduled_backup_dropbox_chunked_upload_uses_session(self):
        server = self._create_server("dropbox")
        record = self._create_record(server, chunk_size=1, backup_lifespan_qty=-1)
        megabyte = 1024 * 1024
        dbx = MagicMock()
        dbx.files_upload_session_finish.return_value = SimpleNamespace(id="id:new")

        def generate_big_backup(db_name, file, extension, bu_type):
            return io.BytesIO(b"x" * (2 * megabyte + 10))

        with self._mock_backup(side_effect=generate_big_backup), patch.object(
            type(self.BackupServer), "provider_authenticate", return_value=dbx
        ), patch.dict(sys.modules, {"dropbox": MagicMock()}):
            self._run_scheduled(record)

        self.assertIn("RESULT TYPE: success", record.last_execution_result)
        dbx.files_upload.assert_not_called()
        self.assertEqual(
            len(dbx.files_upload_session_start.call_args.args[0]), megabyte
        )
        appended = [
            len(append.args[0])
            for append in dbx.files_upload_session_append_v2.call_args_list
        ]
        self.assertEqual(appended, [megabyte, 10])
        dbx.files_upload_session_finish.assert_called_once()
        dbx.files_list_folder.assert_not_called()

    # Manual execution
    def test_manual_execution_returns_notification_without_storing_result(self):
        self._use_temp_destination()
        self._confirm_record()
        with self._mock_backup():
            action = self.record.manual_execution()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "success")
        self.assertFalse(self.record.last_execution_result)

    def test_manual_execution_as_backup_user_succeeds(self):
        tmp_dir = self._use_temp_destination()
        self._confirm_record()
        with self._mock_backup():
            action = self.record.with_user(self.backup_user).manual_execution()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(len(os.listdir(tmp_dir)), 1)

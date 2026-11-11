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
Tests for backup.server model.

TestBackupServer:
  - test_name_unique_per_company_raises_integrity_error
  - test_server_port_not_numeric_raises_validation_error
  - test_server_port_out_of_range_raises_validation_error
  - test_credentials_ok_local_true_with_destination_path
  - test_credentials_ok_sftp_false_without_password
  - test_credentials_ok_drive_text_false_without_parent_folder
  - test_credentials_ok_drive_text_true_with_parent_folder
  - test_credential_fields_not_readable_by_backup_user
  - test_credentials_ok_readable_by_backup_user
  - test_confirm_policy_disabled_raises_user_error
  - test_confirm_resumes_paused_records
  - test_revert_state_pauses_confirmed_records
  - test_archive_sets_archived_state_and_pauses_records
  - test_unarchive_sets_draft_state
  - test_copy_appends_suffix_and_drops_credentials
  - test_get_file_path_details_unsupported_extension_raises_validation_error
  - test_get_file_path_details_appends_trailing_slash
  - test_get_credentials_data_invalid_json_raises_validation_error
  - test_provider_authenticate_local_type_raises_user_error
  - test_provider_authenticate_dropbox_without_token_raises_user_error
  - test_test_connection_sftp_success_confirms_server
  - test_test_connection_sftp_authentication_error_returns_warning
  - test_test_connection_sftp_failure_returns_danger
  - test_test_connection_local_returns_warning
  - test_send_test_file_local_writes_file
  - test_send_test_file_sftp_failure_closes_connection
"""

import os

from unittest.mock import MagicMock, patch

from paramiko import AuthenticationException
from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.eqp_backups.models.backup_server import get_credentials_data

from .common import EqpBackupsCommon


@tagged("post_install", "-at_install")
class TestBackupServer(EqpBackupsCommon):
    """Backup destinations: validation, states, credentials and connection tests."""

    def _mock_sftp(self, **kwargs):
        """Mock the SFTP connection of the servers."""
        return patch.object(
            type(self.BackupServer), "establish_sftp_connection", **kwargs
        )

    # Constraints
    def test_name_unique_per_company_raises_integrity_error(self):
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self.BackupServer.create({"name": self.server.name, "backup_type": "local"})

    def test_server_port_not_numeric_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            self._create_server("sftp", confirm=False, server_port="abc")

    def test_server_port_out_of_range_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            self._create_server("sftp", confirm=False, server_port="70000")

    # Credentials
    def test_credentials_ok_local_true_with_destination_path(self):
        self.assertTrue(self.server.credentials_ok)

    def test_credentials_ok_sftp_false_without_password(self):
        server = self._create_server("sftp", confirm=False, server_password=False)
        self.assertFalse(server.credentials_ok)

    def test_credentials_ok_drive_text_false_without_parent_folder(self):
        server = self._create_server("drive", confirm=False, parent_folder=False)
        self.assertFalse(server.credentials_ok)

    def test_credentials_ok_drive_text_true_with_parent_folder(self):
        server = self._create_server("drive", confirm=False)
        self.assertTrue(server.credentials_ok)

    def test_credential_fields_not_readable_by_backup_user(self):
        server = self._create_server("sftp").with_user(self.backup_user)
        credential_fields = (
            "server_password",
            "drive_credentials_file",
            "drive_credentials_input",
            "dropbox_app_key",
            "dropbox_app_secret",
            "dropbox_app_token",
        )
        for field_name in credential_fields:
            with self.subTest(field=field_name), self.assertRaises(AccessError):
                server.read([field_name])
        self.assertFalse(set(credential_fields) & set(server.fields_get()))

    def test_credentials_ok_readable_by_backup_user(self):
        server = self._create_server("sftp").with_user(self.backup_user)
        self.assertTrue(server.credentials_ok)

    # States
    def test_confirm_policy_disabled_raises_user_error(self):
        server = self._create_server("sftp", confirm=False)
        self.company.eqp_backups_enable_sftp = False
        with self.assertRaises(UserError):
            server.confirm()

    def test_confirm_resumes_paused_records(self):
        self._confirm_record()
        self.server.revert_state()
        self.server.confirm()
        self.assertEqual(self.record.state, "confirmed")
        self.assertTrue(self.record.cron_id.active)

    def test_revert_state_pauses_confirmed_records(self):
        self._confirm_record()
        self.server.revert_state()
        self.assertEqual(self.server.state, "draft")
        self.assertEqual(self.record.state, "paused")
        self.assertFalse(self.record.cron_id.active)

    def test_archive_sets_archived_state_and_pauses_records(self):
        self._confirm_record()
        self.server.action_archive()
        self.assertEqual(self.server.state, "archived")
        self.assertEqual(self.record.state, "paused")
        self.assertFalse(self.record.cron_id.active)

    def test_unarchive_sets_draft_state(self):
        self.server.action_archive()
        self.server.action_unarchive()
        self.assertEqual(self.server.state, "draft")

    def test_copy_appends_suffix_and_drops_credentials(self):
        server = self._create_server("sftp")
        new_server = server.copy()
        self.assertEqual(new_server.name, f"{server.name} (copy)")
        self.assertEqual(new_server.state, "draft")
        self.assertFalse(new_server.server_password)

    # Helpers
    def test_get_file_path_details_unsupported_extension_raises_validation_error(self):
        with self.assertRaises(ValidationError), mute_logger(
            "odoo.addons.eqp_backups.models.backup_server"
        ):
            self.server.get_file_path_details("test", "exe")

    def test_get_file_path_details_appends_trailing_slash(self):
        self.server.destination_path = "/backups"
        path, name = self.server.get_file_path_details("mydb", "zip")
        self.assertEqual(path, "/backups/")
        self.assertRegex(
            name, r"^Backup_mydb_\d{4}-\d{2}-\d{2}_\d{2}\.\d{2}\.\d{2}\.zip$"
        )

    def test_get_credentials_data_invalid_json_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            get_credentials_data("{not json")

    def test_provider_authenticate_local_type_raises_user_error(self):
        with self.assertRaises(UserError):
            self.server.provider_authenticate()

    def test_provider_authenticate_dropbox_without_token_raises_user_error(self):
        server = self._create_server("dropbox", confirm=False, dropbox_app_token=False)
        with self.assertRaises(UserError):
            server.provider_authenticate()

    # Connection tests
    def test_test_connection_sftp_success_confirms_server(self):
        server = self._create_server("sftp", confirm=False)
        sftp, transport = MagicMock(), MagicMock()
        with self._mock_sftp(return_value=(sftp, transport)):
            action = server.with_context(confirm=True).test_connection()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(server.state, "confirmed")
        sftp.close.assert_called_once()
        transport.close.assert_called_once()

    @mute_logger("odoo.addons.eqp_backups.models.backup_server")
    def test_test_connection_sftp_authentication_error_returns_warning(self):
        server = self._create_server("sftp", confirm=False)
        with self._mock_sftp(side_effect=AuthenticationException("bad password")):
            action = server.with_context(confirm=True).test_connection()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertEqual(server.state, "draft")

    @mute_logger("odoo.addons.eqp_backups.models.backup_server")
    def test_test_connection_sftp_failure_returns_danger(self):
        server = self._create_server("sftp", confirm=False)
        with self._mock_sftp(side_effect=OSError("unreachable")):
            action = server.with_context(confirm=True).test_connection()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("unreachable", action["params"]["message"])
        self.assertEqual(server.state, "draft")

    def test_test_connection_local_returns_warning(self):
        action = self.server.test_connection()
        self.assertEqual(action["params"]["type"], "warning")

    def test_send_test_file_local_writes_file(self):
        tmp_dir = self._use_temp_destination()
        action = self.server.send_test_file()
        self.assertEqual(action["params"]["type"], "success")
        files = os.listdir(tmp_dir)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^Backup_test_.*\.txt$")

    @mute_logger("odoo.addons.eqp_backups.models.backup_server")
    def test_send_test_file_sftp_failure_closes_connection(self):
        server = self._create_server("sftp")
        sftp, transport = MagicMock(), MagicMock()
        sftp.file.side_effect = OSError("read-only file system")
        with self._mock_sftp(return_value=(sftp, transport)):
            action = server.send_test_file()
        self.assertEqual(action["params"]["type"], "danger")
        sftp.close.assert_called_once()
        transport.close.assert_called_once()

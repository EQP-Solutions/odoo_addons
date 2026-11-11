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

import io
import shutil
import tempfile

from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests.common import TransactionCase, new_test_user

LIST_DBS = "odoo.addons.eqp_backups.models.backup_record.db.list_dbs"
IMPORT_MODULE = "odoo.addons.eqp_backups.models.res_company.importlib.import_module"
BACKUP_CONTENT = b"backup-content"


def fake_generate_backup(db_name, file, extension, bu_type):
    """Stand-in for `backup.record._generate_backup`: never dumps a real database."""
    if file is None:
        return io.BytesIO(BACKUP_CONTENT)
    file.write(BACKUP_CONTENT)
    return file


class EqpBackupsCommon(TransactionCase):
    """Shared fixtures for eqp_backups tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Enabling Drive/Dropbox checks that the optional libraries are installed
        with patch(IMPORT_MODULE):
            cls.company.write(
                {
                    "eqp_backups_enable_local": True,
                    "eqp_backups_enable_sftp": True,
                    "eqp_backups_enable_drive": True,
                    "eqp_backups_enable_dropbox": True,
                    "eqp_backups_enable_success_email": True,
                    "eqp_backups_enable_failure_email": True,
                }
            )
        cls.db_name = cls.env.cr.dbname
        cls.BackupRecord = cls.env["backup.record"]
        cls.BackupServer = cls.env["backup.server"]

        cls.server = cls.BackupServer.create(
            {
                "name": "Common Local Server",
                "backup_type": "local",
                "destination_path": "/tmp/eqp_backups_tests_unused",
            }
        )
        cls.server.confirm()
        cls.record = cls.BackupRecord.create(
            {
                "name": "Common Backup Record",
                "db_name": cls.db_name,
                "frequency": "days",
                "server_id": cls.server.id,
                "backup_lifespan_qty": 2,
            }
        )
        cls.backup_user = new_test_user(
            cls.env,
            login="eqp_backups_user",
            groups="base.group_user,eqp_backups.group_eqp_backups_user",
        )

    def _use_temp_destination(self, server=None):
        """Point the server to a temporary folder removed after the test."""
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        (server or self.server).destination_path = tmp_dir
        return tmp_dir

    def _create_server(self, backup_type, confirm=True, **values):
        """Create a (confirmed) server of the given type with valid credentials."""
        defaults = {
            "sftp": {
                "destination_path": "/backups",
                "server_address": "sftp.example.com",
                "server_port": "22",
                "server_user": "odoo",
                "server_password": "sftp-pass",
            },
            "drive": {
                "parent_folder": "folder-id",
                "drive_credentials_type": "text",
                "drive_credentials_input": "{}",
            },
            "dropbox": {
                "destination_path": "/backups",
                "dropbox_app_key": "app-key",
                "dropbox_app_secret": "app-secret",
                "dropbox_app_token": "app-token",
            },
        }.get(backup_type, {})
        server = self.BackupServer.create(
            {
                "name": f"Test {backup_type} server",
                "backup_type": backup_type,
                **defaults,
                **values,
            }
        )
        if confirm:
            server.confirm()
        return server

    def _confirm_record(self, record=None):
        """Confirm the backup record, which creates its cron."""
        record = record or self.record
        with patch(LIST_DBS, return_value=[record.db_name]):
            return record.with_context(confirm=True).validate_db()

    @contextmanager
    def _mock_backup(self, dbs=None, side_effect=fake_generate_backup):
        """Mock the database listing and the dump generation."""
        dbs = [self.db_name] if dbs is None else dbs
        with patch(LIST_DBS, return_value=dbs), patch.object(
            type(self.BackupRecord), "_generate_backup", side_effect=side_effect
        ) as generate_backup:
            yield generate_backup

    def _backup_name(self, date):
        """Return a backup file name of the test database for the given date."""
        return f"Backup_{self.db_name}_{date}_00.00.00.zip"

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
Tests for backup.dropbox.token.assignment.wizard model (Dropbox authorization flow).

TestBackupDropboxWizard:
  - test_generate_dropbox_token_returns_wizard_action
  - test_generate_dropbox_token_without_app_secret_raises_user_error
  - test_set_new_token_stores_refresh_token_on_server
  - test_set_new_token_without_access_code_raises_user_error
  - test_set_new_token_on_non_dropbox_server_raises_user_error
  - test_set_new_token_flow_failure_raises_user_error
  - test_wizard_not_accessible_by_backup_user
"""

import sys

from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import EqpBackupsCommon


@tagged("post_install", "-at_install")
class TestBackupDropboxWizard(EqpBackupsCommon):
    """Dropbox OAuth flow: authorization URL and refresh token assignment."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env["backup.dropbox.token.assignment.wizard"]
        cls.dropbox_server = cls.BackupServer.create(
            {
                "name": "Dropbox Server",
                "backup_type": "dropbox",
                "destination_path": "/backups",
                "dropbox_app_key": "app-key",
                "dropbox_app_secret": "app-secret",
            }
        )

    def setUp(self):
        super().setUp()
        # Never reach the Dropbox API
        self.dropbox = MagicMock()
        self.flow = self.dropbox.oauth.DropboxOAuth2FlowNoRedirect
        patcher = patch.dict(sys.modules, {"dropbox": self.dropbox})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create_wizard(self, server=None, access_code="  access-code "):
        return self.Wizard.create(
            {
                "server_id": (server or self.dropbox_server).id,
                "dropbox_auth_url": "https://dropbox.example.com/authorize",
                "dropbox_access_code": access_code,
            }
        )

    def test_generate_dropbox_token_returns_wizard_action(self):
        self.flow.return_value.start.return_value = "https://dropbox.example.com/auth"
        action = self.dropbox_server.generate_dropbox_token()
        self.flow.assert_called_once_with(
            "app-key", "app-secret", token_access_type="offline"
        )
        self.assertEqual(action["res_model"], self.Wizard._name)
        self.assertEqual(
            action["context"],
            {
                "default_dropbox_auth_url": "https://dropbox.example.com/auth",
                "default_server_id": self.dropbox_server.id,
            },
        )

    def test_generate_dropbox_token_without_app_secret_raises_user_error(self):
        self.dropbox_server.dropbox_app_secret = False
        with self.assertRaises(UserError):
            self.dropbox_server.generate_dropbox_token()
        self.flow.assert_not_called()

    def test_set_new_token_stores_refresh_token_on_server(self):
        self.flow.return_value.finish.return_value.refresh_token = "refresh-token"
        self._create_wizard().set_new_token()
        self.flow.return_value.finish.assert_called_once_with("access-code")
        self.assertEqual(self.dropbox_server.dropbox_app_token, "refresh-token")
        self.assertTrue(self.dropbox_server.credentials_ok)

    def test_set_new_token_without_access_code_raises_user_error(self):
        with self.assertRaises(UserError):
            self._create_wizard(access_code=False).set_new_token()
        self.flow.assert_not_called()

    def test_set_new_token_on_non_dropbox_server_raises_user_error(self):
        with self.assertRaises(UserError):
            self._create_wizard(server=self.server).set_new_token()
        self.flow.assert_not_called()

    def test_set_new_token_flow_failure_raises_user_error(self):
        self.flow.return_value.finish.side_effect = Exception("invalid_grant")
        with self.assertRaisesRegex(UserError, "invalid_grant"):
            self._create_wizard().set_new_token()
        self.assertFalse(self.dropbox_server.dropbox_app_token)

    def test_wizard_not_accessible_by_backup_user(self):
        with self.assertRaises(AccessError):
            self.Wizard.with_user(self.backup_user).create(
                {"server_id": self.dropbox_server.id}
            )

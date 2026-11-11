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
Tests for the res.company backup policies.

TestResCompany:
  - test_enable_dropbox_without_library_raises_validation_error
  - test_enable_drive_without_library_raises_validation_error
  - test_enable_local_without_optional_libraries_succeeds
"""

from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import EqpBackupsCommon, IMPORT_MODULE


@tagged("post_install", "-at_install")
class TestResCompany(EqpBackupsCommon):
    """Company policies depending on optional Python libraries."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_company = cls.env["res.company"].create({"name": "Backup Co"})

    def test_enable_dropbox_without_library_raises_validation_error(self):
        with patch(IMPORT_MODULE, side_effect=ImportError("dropbox")):
            with self.assertRaises(ValidationError):
                self.other_company.eqp_backups_enable_dropbox = True

    def test_enable_drive_without_library_raises_validation_error(self):
        with patch(IMPORT_MODULE, side_effect=ImportError("google")):
            with self.assertRaises(ValidationError):
                self.other_company.eqp_backups_enable_drive = True

    def test_enable_local_without_optional_libraries_succeeds(self):
        with patch(IMPORT_MODULE, side_effect=ImportError("missing")):
            self.other_company.write(
                {"eqp_backups_enable_local": True, "eqp_backups_enable_sftp": True}
            )
        self.assertTrue(self.other_company.eqp_backups_enable_local)

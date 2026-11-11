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

from odoo import fields, models, _
from odoo.exceptions import UserError


class BackupDropboxTokenAssignmentWizard(models.TransientModel):
    """
    This model represents a wizard for assigning a new Dropbox token.

    It allows the user to enter a Dropbox access code obtained during the authorization process
    and sets a new token for a related backup server.
    """

    _name = "backup.dropbox.token.assignment.wizard"
    _description = "Dropbox Token Assignment"

    server_id = fields.Many2one("backup.server", string="Related Server")
    dropbox_auth_url = fields.Char(
        string="Authentication URL",
        help="Please complete the authorization process by following the instructions "
        "provided at the following URL.",
    )
    dropbox_access_code = fields.Char(
        string="Access Code",
        help="Please enter the access code received after completing the authorization "
        "process via the URL displayed at the top of this window.",
    )

    def set_new_token(self):
        """
        Sets a new Dropbox token for the related backup server using the provided access code.

        Raises:
            UserError: If the access code is missing or if an error occurs during token generation.
        """
        self.ensure_one()
        dropbox_access_code = self.dropbox_access_code
        server = self.server_id

        if not dropbox_access_code:
            raise UserError(
                _(
                    "Error: Missing Dropbox Access Code. Please ensure set the Access Code to set a New "
                    "Token."
                )
            )
        if server.backup_type != "dropbox":
            raise UserError(
                _("Error: The token can only be assigned to a Dropbox Backup Server.")
            )

        try:
            import dropbox

            dbx_auth = dropbox.oauth.DropboxOAuth2FlowNoRedirect(
                server.dropbox_app_key,
                server.dropbox_app_secret,
                token_access_type="offline",
            )
            dbx_oauth = dbx_auth.finish(dropbox_access_code.strip())
        except Exception as e:
            raise UserError(_("Error: %s.", e))

        server.dropbox_app_token = dbx_oauth.refresh_token

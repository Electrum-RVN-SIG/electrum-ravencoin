# Copyright (C) 2022 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

import asyncio
from collections import defaultdict

from decimal import Decimal
from typing import Optional, TYPE_CHECKING, Sequence, List, Callable, Union
from PyQt5.QtCore import pyqtSignal, QPoint, QSize, Qt
from PyQt5.QtWidgets import (QLabel, QVBoxLayout, QGridLayout, QHBoxLayout, QComboBox,
                             QWidget, QToolTip, QPushButton, QApplication)
from PyQt5.QtGui import QMovie, QColor

from electrum.i18n import _
from electrum.logging import Logger

from electrum import constants
from electrum.asset import DEFAULT_ASSET_AMOUNT_MAX, parse_verifier_string
from electrum.bitcoin import COIN, b58_address_to_hash160, DummyAddress
from electrum.plugin import run_hook
from electrum.util import NotEnoughFunds, NoDynamicFeeEstimates, parse_max_spend
from electrum.invoices import PR_PAID, Invoice, PR_BROADCASTING, PR_BROADCAST
from electrum.transaction import Transaction, PartialTxInput, PartialTxOutput
from electrum.network import TxBroadcastError, BestEffortRequestFailed
from electrum.payment_identifier import PaymentIdentifierState, PaymentIdentifierType, PaymentIdentifier, \
    invoice_from_payment_identifier, payment_identifier_from_invoice
from electrum.wallet import Multisig_Wallet

from .asset_management_panel import AssetAmountEdit
from .amountedit import AmountEdit, BTCAmountEdit, SizedFreezableLineEdit
from .paytoedit import InvalidPaymentIdentifier
from .util import (WaitingDialog, HelpLabel, MessageBoxMixin, EnterButton,
                   char_width_in_lineedit, get_iconname_camera, get_iconname_qrcode,
                   read_QIcon, ColorScheme, icon_path)
from .confirm_tx_dialog import ConfirmTxDialog
from .invoice_list import InvoiceList

if TYPE_CHECKING:
    from .main_window import ElectrumWindow


class SendTab(QWidget, MessageBoxMixin, Logger):

    resolve_done_signal = pyqtSignal(object)
    finalize_done_signal = pyqtSignal(object)
    notify_merchant_done_signal = pyqtSignal(object)

    def __init__(self, window: 'ElectrumWindow'):
        QWidget.__init__(self, window)
        Logger.__init__(self)
        self.app = QApplication.instance()
        self.window = window
        self.wallet = window.wallet
        self.fx = window.fx
        self.config = window.config
        self.network = window.network

        self.format_amount_and_units = window.format_amount_and_units
        self.format_amount = window.format_amount
        self.base_unit = window.base_unit

        self.pending_invoice = None

        # A 4-column grid layout.  All the stretch is in the last column.
        # The exchange rate plugin adds a fiat widget in column 2
        self.send_grid = grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnStretch(3, 1)

        self.pay_selector = QComboBox()
        self.pay_selector.setMaxVisibleItems(10)
        self.pay_selector.setStyleSheet("QComboBox { combobox-popup: 0; }")
        self.current_balance = self.window.wallet.get_balance(asset_aware=True)
        assets = [constants.net.SHORT_NAME]
        assets.extend(sorted(k for k in self.current_balance.keys() if k))
        self.pay_selector.addItems(assets)
        self.pay_selector.currentIndexChanged.connect(self._on_combo_update)

        self.is_hardware = self.wallet.keystore and self.wallet.keystore.get_type_text()[:2] == 'hw'
        self.is_multisig_and_bad = isinstance(self.wallet, Multisig_Wallet) and not constants.net.MULTISIG_ASSETS

        self.handle_assets = not self.is_hardware and not self.is_multisig_and_bad

        if self.handle_assets:
            grid.addWidget(QLabel(_('Asset')), 5, 0)
            grid.addWidget(self.pay_selector, 5, 1, 1, 3)

        from .paytoedit import PayToEdit
        self.amount_e = AssetAmountEdit(lambda: self.pay_selector.currentText()[:4], 8, DEFAULT_ASSET_AMOUNT_MAX * COIN)
        self.payto_e = PayToEdit(self)
        msg = (_("Recipient of the funds.") + "\n\n"
               + _("You may enter a Bitcoin address, a label from your list of contacts "
                   "(a list of completions will be proposed), "
                   "or an alias (email-like address that forwards to a Bitcoin address)") + ". "
               + _("Lightning invoices are also supported.") + "\n\n"
               + _("You can also pay to many outputs in a single transaction, "
                   "specifying one output per line.") + "\n" + _("Format: address, amount") + "\n"
               + _("To set the amount to 'max', use the '!' special character.") + "\n"
               + _("Integers weights can also be used in conjunction with '!', "
                   "e.g. set one amount to '2!' and another to '3!' to split your coins 40-60."))
        payto_label = HelpLabel(_('Pay to'), msg)
        grid.addWidget(payto_label, 0, 0)
        grid.addWidget(self.payto_e, 0, 1, 1, 4)

        #completer = QCompleter()
        #completer.setCaseSensitivity(False)
        #self.payto_e.set_completer(completer)
        #completer.setModel(self.window.completions)

        msg = _('Description of the transaction (not mandatory).') + '\n\n' \
              + _(
            'The description is not sent to the recipient of the funds. It is stored in your wallet file, and displayed in the \'History\' tab.')
        description_label = HelpLabel(_('Description'), msg)
        grid.addWidget(description_label, 1, 0)
        self.message_e = SizedFreezableLineEdit(width=600)
        grid.addWidget(self.message_e, 1, 1, 1, 4)

        msg = _('Comment for recipient')
        self.comment_label = HelpLabel(_('Comment'), msg)
        grid.addWidget(self.comment_label, 2, 0)
        self.comment_e = SizedFreezableLineEdit(width=600)
        grid.addWidget(self.comment_e, 2, 1, 1, 4)
        self.comment_label.hide()
        self.comment_e.hide()

        msg = (_('The amount to be received by the recipient.') + ' '
               + _('Fees are paid by the sender.') + '\n\n'
               + _('The amount will be displayed in red if you do not have enough funds in your wallet.') + ' '
               + _('Note that if you have frozen some of your addresses, the available funds will be lower than your total balance.') + '\n\n'
               + _('Keyboard shortcut: type "!" to send all your coins.'))
        amount_label = HelpLabel(_('Amount'), msg)
        grid.addWidget(amount_label, 3, 0)
        grid.addWidget(self.amount_e, 3, 1)

        self.fiat_send_e = AmountEdit(self.fx.get_currency if self.fx else '')
        if not self.fx or not self.fx.is_enabled():
            self.fiat_send_e.setVisible(False)
        grid.addWidget(self.fiat_send_e, 3, 2)
        self.amount_e.frozen.connect(
            lambda: self.fiat_send_e.setFrozen(self.amount_e.isReadOnly()))

        self.window.connect_fields(self.amount_e, self.fiat_send_e)

        self.max_button = EnterButton(_("Max"), self.spend_max)
        btn_width = 10 * char_width_in_lineedit()
        self.max_button.setFixedWidth(btn_width)
        self.max_button.setCheckable(True)
        self.max_button.setEnabled(False)
        grid.addWidget(self.max_button, 3, 3)

        self.paste_button = QPushButton()
        self.paste_button.clicked.connect(self.do_paste)
        self.paste_button.setIcon(read_QIcon('copy.png'))
        self.paste_button.setToolTip(_('Paste invoice from clipboard'))
        self.paste_button.setMaximumWidth(35)
        grid.addWidget(self.paste_button, 0, 5)

        self.spinner = QMovie(icon_path('spinner.gif'))
        self.spinner.setScaledSize(QSize(24, 24))
        self.spinner.setBackgroundColor(QColor('black'))
        self.spinner_l = QLabel()
        self.spinner_l.setMargin(5)
        self.spinner_l.setVisible(False)
        self.spinner_l.setMovie(self.spinner)
        grid.addWidget(self.spinner_l, 0, 1, 1, 4, Qt.AlignRight)

        self.save_button = EnterButton(_("Save"), self.do_save_invoice)
        self.save_button.setEnabled(False)
        self.send_button = EnterButton(_("Pay") + "...", self.do_pay_or_get_invoice)
        self.send_button.setEnabled(False)
        self.clear_button = EnterButton(_("Clear"), self.do_clear)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.send_button)
        grid.addLayout(buttons, 6, 1, 1, 4)

        self.amount_e.shortcut.connect(self.spend_max)

        def reset_max(text):
            self.max_button.setChecked(False)

        self.amount_e.textChanged.connect(self.on_amount_changed)
        self.amount_e.textEdited.connect(reset_max)
        self.fiat_send_e.textEdited.connect(reset_max)

        self.invoices_label = QLabel(_('Invoices'))
        self.invoice_list = InvoiceList(self)
        self.toolbar, menu = self.invoice_list.create_toolbar_with_menu('')

        menu.addAction(read_QIcon(get_iconname_camera()),    _("Read QR code with camera"), self.payto_e.on_qr_from_camera_input_btn)
        menu.addAction(read_QIcon("picture_in_picture.png"), _("Read QR code from screen"), self.payto_e.on_qr_from_screenshot_input_btn)
        menu.addAction(read_QIcon("file.png"), _("Read invoice from file"), self.payto_e.on_input_file)
        self.paytomany_menu = menu.addToggle(_("&Pay to many"), self.toggle_paytomany)
        menu.addSeparator()
        menu.addAction(_("Import invoices"), self.window.import_invoices)
        menu.addAction(_("Export invoices"), self.window.export_invoices)

        vbox0 = QVBoxLayout()
        vbox0.addLayout(grid)
        hbox = QHBoxLayout()
        hbox.addLayout(vbox0)
        hbox.addStretch(1)

        vbox = QVBoxLayout(self)
        vbox.addLayout(self.toolbar)
        vbox.addLayout(hbox)
        vbox.addStretch(1)
        vbox.addWidget(self.invoices_label)
        vbox.addWidget(self.invoice_list)
        vbox.setStretchFactor(self.invoice_list, 1000)
        self.searchable_list = self.invoice_list
        self.invoice_list.update()  # after parented and put into a layout, can update without flickering
        run_hook('create_send_tab', grid)

        self.resolve_done_signal.connect(self.on_resolve_done)
        self.finalize_done_signal.connect(self.on_finalize_done)
        self.notify_merchant_done_signal.connect(self.on_notify_merchant_done)
        self.payto_e.paymentIdentifierChanged.connect(self._handle_payment_identifier)

    def showSpinner(self, b):
        self.spinner_l.setVisible(b)
        if b:
            self.spinner.start()
        else:
            self.spinner.stop()

    def on_amount_changed(self, text):
        # FIXME: implement full valid amount check to enable/disable Pay button
        pi = self.payto_e.payment_identifier
        if not pi:
            self.send_button.setEnabled(False)
            return
        pi_error = pi.is_error() if pi.is_valid() else False
        is_spk_script = pi.type == PaymentIdentifierType.SPK and not pi.spk_is_address
        valid_amount = is_spk_script or bool(self.amount_e.get_amount())
        self.send_button.setEnabled(pi.is_valid() and not pi_error and valid_amount)

    def do_paste(self):
        self.logger.debug('do_paste')
        try:
            self.payto_e.try_payment_identifier(self.app.clipboard().text())
        except InvalidPaymentIdentifier as e:
            self.show_error(_('Invalid payment identifier on clipboard'))

    def set_payment_identifier(self, text):
        self.logger.debug('set_payment_identifier')
        try:
            self.payto_e.try_payment_identifier(text)
        except InvalidPaymentIdentifier as e:
            self.show_error(_('Invalid payment identifier'))

    def update(self):
        self.invoice_list.update()
        if self.handle_assets:
            current_balance = self.window.wallet.get_balance(asset_aware=True)
            if self.current_balance.keys() != current_balance.keys():
                assets = [constants.net.SHORT_NAME]
                assets.extend(sorted(k for k in current_balance.keys() if k))
                current_text = self.pay_selector.currentText()
                self.pay_selector.clear()
                self.pay_selector.addItems(assets)
                try:
                    i = assets.index(current_text)
                    self.pay_selector.setCurrentIndex(i)
                except ValueError:
                    pass
        self._on_combo_update()
        super().update()

    def _selected_asset(self) -> Optional[str]:
        return None if self.pay_selector.currentIndex() == 0 else self.pay_selector.currentText()
    
    def _on_combo_update(self):
        divisions = 8
        asset = self._selected_asset()
        if asset:
            metadata_tup = self.window.wallet.adb.get_asset_metadata(asset)
            divisions = metadata_tup[0].divisions
            self.fiat_send_e.setVisible(False)
        else:
            self.fiat_send_e.setVisible(self.fx and self.fx.is_enabled())
        self.amount_e.divisions = divisions
        self.amount_e.is_int = divisions == 0
        self.amount_e.numbify()
        self.amount_e.update()
        self.payto_e._handle_text_change(force=True)
        if self.max_button.isChecked():
            self.spend_max()

    def spend_max(self):
        pi = self.payto_e.payment_identifier

        if pi is None or pi.type == PaymentIdentifierType.UNKNOWN:
            return

        assert pi.type in [PaymentIdentifierType.SPK, PaymentIdentifierType.MULTILINE,
                           PaymentIdentifierType.BIP21, PaymentIdentifierType.OPENALIAS]

        if pi.type == PaymentIdentifierType.BIP21:
            assert 'amount' not in pi.bip21

        if run_hook('abort_send', self):
            return
        outputs = pi.get_onchain_outputs('!')
        if not outputs:
            return
        assets = {output.asset for output in outputs}.union({None})
        make_tx = lambda fee_est, *, confirmed_only=False: self.wallet.make_unsigned_transaction(
            coins=[coin for coin in self.window.get_coins() if coin.asset in assets],
            outputs=outputs,
            fee=fee_est,
            is_sweep=False)
        try:
            try:
                tx = make_tx(None)
            except (NotEnoughFunds, NoDynamicFeeEstimates) as e:
                # Check if we had enough funds excluding fees,
                # if so, still provide opportunity to set lower fees.
                tx = make_tx(0)
        except NotEnoughFunds as e:
            self.max_button.setChecked(False)
            text = self.window.get_text_not_enough_funds_mentioning_frozen()
            self.show_error(text)
            return

        self.max_button.setChecked(True)
        amount = tx.output_value(asset_aware=True)[self._selected_asset()]
        __, x_fee_amount = run_hook('get_tx_extra_fee', self.wallet, tx) or (None, 0)
        amount_after_all_fees = amount - (x_fee_amount if self._selected_asset() else 0)
        self.amount_e.setAmount(amount_after_all_fees)
        # show tooltip explaining max amount
        mining_fee = tx.get_fee()
        mining_fee_str = self.format_amount_and_units(mining_fee)
        msg = _("Mining fee: {} (can be adjusted on next screen)").format(mining_fee_str)
        if x_fee_amount:
            twofactor_fee_str = self.format_amount_and_units(x_fee_amount)
            msg += "\n" + _("2fa fee: {} (for the next batch of transactions)").format(twofactor_fee_str)
        frozen_bal = self.window.get_frozen_balance_str()
        if frozen_bal:
            msg += "\n" + _("Some coins are frozen: {} (can be unfrozen in the Addresses or in the Coins tab)").format(frozen_bal)
        QToolTip.showText(self.max_button.mapToGlobal(QPoint(0, 0)), msg)

    # TODO: instead of passing outputs, use an invoice instead (like pay_lightning_invoice)
    # so we have more context (we cannot rely on send_tab field contents or payment identifier
    # as this method is called from other places as well).
    def pay_onchain_dialog(
            self,
            outputs: List[PartialTxOutput],
            *,
            nonlocal_only=False,
            external_keypairs=None,
            get_coins: Callable[..., Sequence[PartialTxInput]] = None,
            invoice: Optional[Invoice] = None
    ) -> None:
        # trustedcoin requires this
        if run_hook('abort_send', self):
            return

        payment_identifier = None
        if invoice and invoice.bip70:
            payment_identifier = payment_identifier_from_invoice(self.wallet, invoice)

        is_sweep = bool(external_keypairs)
        # we call get_coins inside make_tx, so that inputs can be changed dynamically
        if get_coins is None:
            get_coins = self.window.get_coins
        assets = {output.asset for output in outputs}.union({None})
        make_tx = lambda fee_est, *, confirmed_only=False: self.wallet.make_unsigned_transaction(
            coins=[coin for coin in get_coins(nonlocal_only=nonlocal_only, confirmed_only=confirmed_only) if coin.asset in assets],
            outputs=outputs,
            fee=fee_est,
            is_sweep=is_sweep)
        output_values = defaultdict(list)
        for output in outputs:
            output_values[output.asset].append(output.asset_aware_value())
        output_value = dict()
        for asset, values in output_values.items():
            is_max = any(parse_max_spend(outval) for outval in values)
            output_value[asset] = '!' if is_max else sum(values)

        conf_dlg = ConfirmTxDialog(window=self.window, make_tx=make_tx, output_value=output_value)
        if conf_dlg.not_enough_funds:
            # note: use confirmed_only=False here, regardless of config setting,
            #       as the user needs to get to ConfirmTxDialog to change the config setting
            if not conf_dlg.can_pay_assuming_zero_fees(confirmed_only=False):
                text = self.window.get_text_not_enough_funds_mentioning_frozen()
                self.show_message(text)
                return
        tx = conf_dlg.run()
        if tx is None:
            # user cancelled
            return
        is_preview = conf_dlg.is_preview

        if tx.has_dummy_output(DummyAddress.SWAP):
            sm = self.wallet.lnworker.swap_manager
            coro = sm.request_swap_for_tx(tx)
            swap, invoice, tx = self.network.run_from_another_thread(coro)
            assert not tx.has_dummy_output(DummyAddress.SWAP)
            tx.swap_invoice = invoice
            tx.swap_payment_hash = swap.payment_hash

        if is_preview:
            self.window.show_transaction(tx, external_keypairs=external_keypairs, payment_identifier=payment_identifier)
            return
        self.save_pending_invoice()
        def sign_done(success):
            if success:
                self.window.broadcast_or_show(tx, payment_identifier=payment_identifier)
        self.window.sign_tx(
            tx,
            callback=sign_done,
            external_keypairs=external_keypairs)

    def do_clear(self):
        self.logger.debug('do_clear')
        self.lock_fields(lock_recipient=False, lock_amount=False, lock_max=True, lock_description=False, lock_asset=False)
        self.max_button.setChecked(False)
        self.payto_e.do_clear()
        for w in [self.comment_e, self.comment_label]:
            w.setVisible(False)
        for w in [self.message_e, self.amount_e, self.fiat_send_e, self.comment_e]:
            w.setText('')
            w.setToolTip('')
        for w in [self.save_button, self.send_button]:
            w.setEnabled(False)
        if self.handle_assets:
            self.pay_selector.setCurrentIndex(0)
        self.window.update_status()
        self.paytomany_menu.setChecked(self.payto_e.multiline)

        run_hook('do_clear', self)

    def prepare_for_send_tab_network_lookup(self):
        for btn in [self.save_button, self.send_button, self.clear_button]:
            btn.setEnabled(False)
        self.showSpinner(True)

    def payment_request_error(self, error):
        self.show_message(error)
        self.do_clear()

    def set_field_validated(self, w, *, validated: Optional[bool] = None):
        if validated is not None:
            w.setStyleSheet(ColorScheme.GREEN.as_stylesheet(True) if validated else ColorScheme.RED.as_stylesheet(True))

    def lock_fields(self, *,
            lock_recipient: Optional[bool] = None,
            lock_amount: Optional[bool] = None,
            lock_max: Optional[bool] = None,
            lock_description: Optional[bool] = None,
            lock_asset: Optional[bool] = None
    ) -> None:
        self.logger.debug(f'locking fields, r={lock_recipient}, a={lock_amount}, m={lock_max}, d={lock_description}, r={lock_asset}')
        if lock_recipient is not None:
            self.payto_e.setFrozen(lock_recipient)
        if lock_amount is not None:
            self.amount_e.setFrozen(lock_amount)
        if lock_max is not None:
            self.max_button.setEnabled(not lock_max)
        if lock_description is not None:
            self.message_e.setFrozen(lock_description)
        if lock_asset is not None:
            self.pay_selector.setEnabled(not lock_asset)

    def update_fields(self):
        self.logger.debug('update_fields')
        pi = self.payto_e.payment_identifier

        self.clear_button.setEnabled(True)

        if pi.is_multiline():
            self.lock_fields(lock_recipient=False, lock_amount=True, lock_max=True, lock_description=False, lock_asset=False)
            self.set_field_validated(self.payto_e, validated=pi.is_valid()) # TODO: validated used differently here than openalias
            self.save_button.setEnabled(pi.is_valid())
            self.send_button.setEnabled(pi.is_valid())
            self.payto_e.setToolTip(pi.get_error() if not pi.is_valid() else '')
            if pi.is_valid():
                self.handle_multiline(pi.multiline_outputs)
            return

        if not pi.is_valid():
            self.lock_fields(lock_recipient=False, lock_amount=False, lock_max=True, lock_description=False, lock_asset=False)
            self.save_button.setEnabled(False)
            self.send_button.setEnabled(False)
            return

        valid_asset = False
        if asset := pi.get_asset():
            if self.is_hardware:
                self.show_message(_('Hardware wallets cannot send assets'))
            else:
                index = self.pay_selector.findText(asset)
                if index < 1:
                    self.show_message(_('You do not own any {}').format(asset))
                else:
                    self.pay_selector.setCurrentIndex(index)
                    valid_asset = True
            if not valid_asset:
                return
        
        lock_recipient = pi.type in [PaymentIdentifierType.LNURLP, PaymentIdentifierType.LNADDR,
                                     PaymentIdentifierType.OPENALIAS, PaymentIdentifierType.BIP70,
                                     PaymentIdentifierType.BIP21, PaymentIdentifierType.BOLT11] and not pi.need_resolve()
        lock_amount = pi.is_amount_locked()
        lock_max = lock_amount or pi.type not in [PaymentIdentifierType.SPK, PaymentIdentifierType.BIP21]

        self.lock_fields(lock_recipient=lock_recipient,
                         lock_amount=lock_amount,
                         lock_max=lock_max,
                         lock_description=False,
                         lock_asset=valid_asset and pi.type in [PaymentIdentifierType.BIP21, PaymentIdentifierType.BIP70])
        if lock_recipient:
            fields = pi.get_fields_for_GUI()
            if fields.recipient:
                self.payto_e.setText(fields.recipient)
            if fields.description:
                self.message_e.setText(fields.description)
                self.lock_fields(lock_description=True)
            if fields.amount:
                self.amount_e.setAmount(fields.amount)
            for w in [self.comment_e, self.comment_label]:
                w.setVisible(bool(fields.comment))
            if fields.comment:
                self.comment_e.setToolTip(_('Max comment length: %d characters') % fields.comment)
            self.set_field_validated(self.payto_e, validated=fields.validated)

            # LNURLp amount range
            if fields.amount_range:
                amin, amax = fields.amount_range
                self.amount_e.setToolTip(_('Amount must be between %d and %d sat.') % (amin, amax))
            else:
                self.amount_e.setToolTip('')

        # resolve '!' in amount editor if it was set before PI
        if not lock_max and self.amount_e.text() == '!':
            self.spend_max()

        pi_unusable = pi.is_error() or (not self.wallet.has_lightning() and not pi.is_onchain())
        is_spk_script = pi.type == PaymentIdentifierType.SPK and not pi.spk_is_address

        amount_valid = is_spk_script or bool(self.amount_e.get_amount())

        self.send_button.setEnabled(not pi_unusable and amount_valid and not pi.has_expired())
        self.save_button.setEnabled(not pi_unusable and not is_spk_script and \
                                    pi.type not in [PaymentIdentifierType.LNURLP, PaymentIdentifierType.LNADDR])

    def _handle_payment_identifier(self):
        self.update_fields()

        if not self.payto_e.payment_identifier.is_valid():
            self.logger.debug(f'PI error: {self.payto_e.payment_identifier.error}')
            return

        if self.payto_e.payment_identifier.need_resolve():
            self.prepare_for_send_tab_network_lookup()
            self.payto_e.payment_identifier.resolve(on_finished=self.resolve_done_signal.emit)

    def on_resolve_done(self, pi):
        # TODO: resolve can happen while typing, we don't want message dialogs to pop up
        # currently we don't set error for emaillike recipients to avoid just that
        self.logger.debug('payment identifier resolve done')
        self.showSpinner(False)
        if pi.error:
            self.show_error(pi.error)
            self.do_clear()
            return
        self.update_fields()

    def get_message(self):
        return self.message_e.text()

    def read_invoice(self) -> Optional[Invoice]:
        if self.check_payto_line_and_show_errors():
            return

        amount_sat = self.read_amount()
        invoice = invoice_from_payment_identifier(
            self.payto_e.payment_identifier, self.wallet, amount_sat, self.get_message())
        if not invoice:
            self.show_error('error getting invoice' + self.payto_e.payment_identifier.error)
            return

        if not self.wallet.has_lightning() and not invoice.can_be_paid_onchain():
            self.show_error(_('Lightning is disabled'))
        if self.wallet.get_invoice_status(invoice) == PR_PAID:
            # fixme: this is only for bip70 and lightning
            self.show_error(_('Invoice already paid'))
            return
        #if not invoice.is_lightning():
        #    if self.check_onchain_outputs_and_show_errors(outputs):
        #        return
        return invoice

    def do_save_invoice(self):
        self.pending_invoice = self.read_invoice()
        if not self.pending_invoice:
            return
        self.save_pending_invoice()

    def save_pending_invoice(self):
        if not self.pending_invoice:
            return
        self.do_clear()
        self.wallet.save_invoice(self.pending_invoice)
        self.invoice_list.update()
        self.pending_invoice = None

    def get_amount(self) -> int:
        # must not be None
        return self.amount_e.get_amount() or 0

    def on_finalize_done(self, pi: PaymentIdentifier):
        self.showSpinner(False)
        self.update_fields()
        if pi.error:
            self.show_error(pi.error)
            return
        invoice = pi.bolt11
        self.pending_invoice = invoice
        self.logger.debug(f'after finalize invoice: {invoice!r}')
        self.do_pay_invoice(invoice)

    def do_pay_or_get_invoice(self):
        pi = self.payto_e.payment_identifier
        if pi.need_finalize():
            self.prepare_for_send_tab_network_lookup()
            pi.finalize(amount_sat=self.get_amount(), comment=self.comment_e.text(),
                        on_finished=self.finalize_done_signal.emit)
            return
        self.pending_invoice = self.read_invoice()
        if not self.pending_invoice:
            return
        self.do_pay_invoice(self.pending_invoice)

    def pay_multiple_invoices(self, invoices):
        outputs = []
        for invoice in invoices:
            outputs += invoice.outputs
        self.pay_onchain_dialog(outputs)

    def do_edit_invoice(self, invoice: 'Invoice'):  # FIXME broken
        assert not bool(invoice.get_amount_sat())
        text = invoice.lightning_invoice if invoice.is_lightning() else invoice.get_address()
        self.set_payment_identifier(text)
        self.amount_e.setFocus()
        # disable save button, because it would create a new invoice
        self.save_button.setEnabled(False)

    def do_pay_invoice(self, invoice: 'Invoice'):
        if not bool(invoice.get_amount_sat()):
            pi = self.payto_e.payment_identifier
            if pi.type == PaymentIdentifierType.SPK and not pi.spk_is_address:
                pass
            else:
                self.show_error(_('No amount'))
                return
        
        # True = valid, False = not; return error
        def validate_address_for_restricted_asset(address: str, asset: str) -> bool:
            result = self.wallet.adb.db.get_verified_restricted_freeze(asset)
            if result and result['frozen'] == True:
                return False

            x, h160 = b58_address_to_hash160(address)
            h160_h = h160.hex()

            # We are sending the asset -> we have the asset -> we watch for tags
            result = self.wallet.adb.is_h160_tagged(h160_h, asset)
            if result and result['tag'] == True:
                return False

            verifier_dict = self.wallet.adb.db.get_verified_restricted_verifier(asset)
            assert verifier_dict
            verifier_string = verifier_dict['string']
            if verifier_string == 'true': return True
            
            node = parse_verifier_string(verifier_string)
            vars = set()
            node.iterate_variables(vars.add)

            is_qualified_for_tag = dict()
            
            if self.wallet.adb.db.is_h160_checked(h160_h):
                for var in vars:
                    for asset, d in self.wallet.adb.get_tags_for_h160(h160_h, include_mempool=False).items():
                        if (asset == f'#{var}' or asset.startswith(f'#{var}/#')) and d['flag']:
                            is_qualified_for_tag[var] = True
                            break
                    else:
                        is_qualified_for_tag[var] = False
            else:            
                if not self.network:
                    return True
                
                async def tag_lookup():
                    try:
                        result = await self.network.get_tags_for_h160(h160_h)
                    except Exception as e:
                        return None
                    return result
                
                coro = tag_lookup()
                fut = asyncio.run_coroutine_threadsafe(coro, self.network.asyncio_loop)
                result = fut.result()
                if result is None:
                    return True

                for var in vars:
                    for asset, d in result.items():
                        if (asset == f'#{var}' or asset.startswith(f'#{var}/#')) and d['flag']:
                            is_qualified_for_tag[var] = True
                            break
                    else:
                        is_qualified_for_tag[var] = False

            return node.evaluate(is_qualified_for_tag)

        if invoice.outputs:
            for output in invoice.outputs:
                asset: Optional[str] = output.asset
                address = output.address
                if asset and address and asset[0] == '$':
                    result = validate_address_for_restricted_asset(address, asset)
                    if not result:
                        self.show_error(_('{} is not qualified to receive restricted asset {}').format(address, asset))
                        return
        elif (address := invoice.get_address()) and (asset := invoice.get_asset()) and asset[0] == '$':
            result = validate_address_for_restricted_asset(address, asset)
            if not result:
                self.show_error(_('{} is not qualified to receive restricted asset {}').format(address, asset))
                return
        
        if invoice.is_lightning():
            self.pay_lightning_invoice(invoice)
        else:
            self.pay_onchain_dialog(invoice.outputs, invoice=invoice)

    def read_amount(self) -> Union[str, int]:
        amount = '!' if self.max_button.isChecked() else self.get_amount()
        return amount

    def check_onchain_outputs_and_show_errors(self, outputs: List[PartialTxOutput]) -> bool:
        """Returns whether there are errors with outputs.
        Also shows error dialog to user if so.
        """
        if not outputs:
            self.show_error(_('No outputs'))
            return True

        for o in outputs:
            if o.scriptpubkey is None:
                self.show_error(_('Bitcoin Address is None'))
                return True
            if o.value is None:
                self.show_error(_('Invalid Amount'))
                return True

        return False  # no errors

    def check_payto_line_and_show_errors(self) -> bool:
        """Returns whether there are errors.
        Also shows error dialog to user if so.
        """
        error = self.payto_e.payment_identifier.get_error()
        if error:
            if not self.payto_e.payment_identifier.is_multiline():
                err = error
                self.show_warning(
                    _("Failed to parse 'Pay to' line") + ":\n" +
                    f"{err.line_content[:40]}...\n\n"
                    f"{err.exc!r}")
            else:
                self.show_warning(
                    _("Invalid Lines found:") + "\n\n" + error)
                #'\n'.join([_("Line #") +
                #               f"{err.idx+1}: {err.line_content[:40]}... ({err.exc!r})"
                #               for err in errors]))
            return True

        warning = self.payto_e.payment_identifier.warning
        if warning:
            warning += '\n' + _('Do you wish to continue?')
            if not self.question(warning):
                return True

        if self.payto_e.payment_identifier.has_expired():
            self.show_error(_('Payment request has expired'))
            return True

        return False  # no errors

    def pay_lightning_invoice(self, invoice: Invoice):
        amount_sat = invoice.get_amount_sat()
        if amount_sat is None:
            raise Exception("missing amount for LN invoice")
        # note: lnworker might be None if LN is disabled,
        #       in which case we should still offer the user to pay onchain.
        lnworker = self.wallet.lnworker
        if lnworker is None or not lnworker.can_pay_invoice(invoice):
            coins = self.window.get_coins(nonlocal_only=True)
            can_pay_onchain = invoice.can_be_paid_onchain() and self.wallet.can_pay_onchain(invoice.get_outputs(), coins=coins)
            can_pay_with_new_channel = False
            can_pay_with_swap = False
            can_rebalance = False
            if lnworker:
                can_pay_with_new_channel = lnworker.suggest_funding_amount(amount_sat, coins=coins)
                can_pay_with_swap = lnworker.suggest_swap_to_send(amount_sat, coins=coins)
                rebalance_suggestion = lnworker.suggest_rebalance_to_send(amount_sat)
                can_rebalance = bool(rebalance_suggestion) and self.window.num_tasks() == 0
            choices = {}
            if can_rebalance:
                msg = ''.join([
                    _('Rebalance existing channels'), '\n',
                    _('Move funds between your channels in order to increase your sending capacity.')
                ])
                choices[0] = msg
            if can_pay_with_new_channel:
                msg = ''.join([
                    _('Open a new channel'), '\n',
                    _('You will be able to pay once the channel is open.')
                ])
                choices[1] = msg
            if can_pay_with_swap:
                msg = ''.join([
                    _('Swap onchain funds for lightning funds'), '\n',
                    _('You will be able to pay once the swap is confirmed.')
                ])
                choices[2] = msg
            if can_pay_onchain:
                msg = ''.join([
                    _('Pay onchain'), '\n',
                    _('Funds will be sent to the invoice fallback address.')
                ])
                choices[3] = msg
            msg = _('You cannot pay that invoice using Lightning.')
            if lnworker and lnworker.channels:
                num_sats_can_send = int(lnworker.num_sats_can_send())
                msg += '\n' + _('Your channels can send {}.').format(self.format_amount(num_sats_can_send) + ' ' + self.base_unit())
            if not choices:
                self.window.show_error(msg)
                return
            r = self.window.query_choice(msg, choices)
            if r is not None:
                self.save_pending_invoice()
                if r == 0:
                    chan1, chan2, delta = rebalance_suggestion
                    self.window.rebalance_dialog(chan1, chan2, amount_sat=delta)
                elif r == 1:
                    amount_sat, min_amount_sat = can_pay_with_new_channel
                    self.window.new_channel_dialog(amount_sat=amount_sat, min_amount_sat=min_amount_sat)
                elif r == 2:
                    chan, swap_recv_amount_sat = can_pay_with_swap
                    self.window.run_swap_dialog(is_reverse=False, recv_amount_sat=swap_recv_amount_sat, channels=[chan])
                elif r == 3:
                    self.pay_onchain_dialog(invoice.get_outputs(), nonlocal_only=True)
            return

        assert lnworker is not None
        # FIXME this is currently lying to user as we truncate to satoshis
        amount_msat = invoice.get_amount_msat()
        msg = _("Pay lightning invoice?") + '\n\n' + _("This will send {}?").format(self.format_amount_and_units(Decimal(amount_msat)/1000))
        if not self.question(msg):
            return
        self.save_pending_invoice()
        coro = lnworker.pay_invoice(invoice.lightning_invoice, amount_msat=amount_msat)
        self.window.run_coroutine_from_thread(coro, _('Sending payment'))

    def broadcast_transaction(self, tx: Transaction, *, payment_identifier: PaymentIdentifier = None):
        # note: payment_identifier is explicitly passed as self.payto_e.payment_identifier might
        #       already be cleared or otherwise have changed.
        
        if hasattr(tx, 'swap_payment_hash'):
            sm = self.wallet.lnworker.swap_manager
            swap = sm.get_swap(tx.swap_payment_hash)
            coro = sm.wait_for_htlcs_and_broadcast(swap, tx.swap_invoice, tx)
            self.window.run_coroutine_dialog(
                coro, _('Awaiting swap payment...'),
                on_result=self.window.on_swap_result,
                on_cancelled=lambda: sm.cancel_normal_swap(swap))
            return

        def broadcast_thread():
            # non-GUI thread
            if payment_identifier and payment_identifier.has_expired():
                return False, _("Invoice has expired")
            try:
                self.network.run_from_another_thread(self.network.broadcast_transaction(tx))
            except TxBroadcastError as e:
                return False, e.get_message_for_gui()
            except BestEffortRequestFailed as e:
                return False, repr(e)
            # success
            txid = tx.txid()
            if payment_identifier and payment_identifier.need_merchant_notify():
                refund_address = self.wallet.get_receiving_address()
                payment_identifier.notify_merchant(
                    tx=tx,
                    refund_address=refund_address,
                    on_finished=self.notify_merchant_done_signal.emit
                )
            return True, txid

        # Capture current TL window; override might be removed on return
        parent = self.window.top_level_window(lambda win: isinstance(win, MessageBoxMixin))

        self.wallet.set_broadcasting(tx, broadcasting_status=PR_BROADCASTING)

        def broadcast_done(result):
            # GUI thread
            if result:
                success, msg = result
                if success:
                    parent.show_message(_('Payment sent.') + '\n' + msg)
                    self.invoice_list.update()
                    self.wallet.set_broadcasting(tx, broadcasting_status=PR_BROADCAST)
                else:
                    msg = msg or ''
                    parent.show_error(msg)
                    self.wallet.set_broadcasting(tx, broadcasting_status=None)

        WaitingDialog(self, _('Broadcasting transaction...'),
                      broadcast_thread, broadcast_done, self.window.on_error)

    def on_notify_merchant_done(self, pi: PaymentIdentifier):
        if pi.is_error():
            self.logger.debug(f'merchant notify error: {pi.get_error()}')
        else:
            self.logger.debug(f'merchant notify result: {pi.merchant_ack_status}: {pi.merchant_ack_message}')
        # TODO: show user? if we broadcasted the tx succesfully, do we care?
        # BitPay complains with a NAK if tx is RbF

    def toggle_paytomany(self):
        self.payto_e.toggle_paytomany()
        if self.payto_e.is_paytomany():
            message = '\n'.join([
                _('Enter a list of outputs in the \'Pay to\' field.'),
                _('One output per line.'),
                _('Format: address, amount'),
                _('You may load a CSV file using the file icon.')
            ])
            self.window.show_tooltip_after_delay(message)

    def payto_contacts(self, labels):
        paytos = [self.window.get_contact_payto(label) for label in labels]
        self.window.show_send_tab()
        self.payto_e.do_clear()
        if len(paytos) == 1:
            self.logger.debug('payto_e setText 1')
            self.payto_e.setText(paytos[0])
            self.amount_e.setFocus()
        else:
            self.payto_e.setFocus()
            text = "\n".join([payto + ", 0" for payto in paytos])
            self.logger.debug('payto_e setText n')
            self.payto_e.setText(text)
            self.payto_e.setFocus()

    def handle_multiline(self, outputs: Sequence[PartialTxOutput]):
        total = 0
        for output in outputs:
            if parse_max_spend(output.value):
                self.max_button.setChecked(True) # TODO: remove and let spend_max set this?
                self.spend_max()
                return
            else:
                total += output.asset_aware_value()
        self.amount_e.setAmount(total if outputs else None)

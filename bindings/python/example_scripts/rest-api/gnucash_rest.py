#!/usr/bin/env python3

'''

gnucash_rest.py -- A Flask app which responds to REST requests
with JSON responses

Copyright (C) 2013 Tom Lofts <dev@loftx.co.uk>

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License as
published by the Free Software Foundation; either version 2 of
the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, contact:

Free Software Foundation Voice: +1-617-542-5942
51 Franklin Street, Fifth Floor Fax: +1-617-542-2652
Boston, MA 02110-1301, USA gnu@gnu.org

@author Tom Lofts <dev@loftx.co.uk>

'''

import gnucash
import gnucash_simple
import json
import atexit
from flask import Flask, abort, request, Response
import sys
import getopt

from decimal import Decimal

from gnucash.gnucash_business import Vendor, Bill, Entry, GncNumeric, \
    Customer, Invoice, Split, Account, Transaction

from gnucash import GncPrice, GncCommodity

from gnucash import gnucash_core_c

import datetime

from gnucash import \
    QOF_QUERY_AND, \
    QOF_QUERY_OR, \
    QOF_QUERY_NAND, \
    QOF_QUERY_NOR, \
    QOF_QUERY_XOR

from gnucash import \
    QOF_STRING_MATCH_NORMAL, \
    QOF_STRING_MATCH_CASEINSENSITIVE

from gnucash import \
    QOF_COMPARE_LT, \
    QOF_COMPARE_LTE, \
    QOF_COMPARE_EQUAL, \
    QOF_COMPARE_GT, \
    QOF_COMPARE_GTE, \
    QOF_COMPARE_NEQ, \
    QOF_COMPARE_CONTAINS

from gnucash import \
    QOF_DATE_MATCH_NORMAL

from gnucash import \
    QOF_NUMERIC_MATCH_ANY

from gnucash import \
    INVOICE_TYPE

from gnucash import \
    INVOICE_IS_PAID

from gnucash import SessionOpenMode

app = Flask(__name__)
app.debug = True

@app.before_request
def require_session():

    # Every route below dereferences the global `session` (directly or via a
    # helper). A failed POST /revert can leave `session` as None, so fail fast
    # here with a clean 503 rather than crashing deep in a handler with an
    # AttributeError. /revert is exempt so it can be retried to recover.
    if session is None and request.endpoint != 'api_revert':
        return Response(json.dumps({'errors': [{'type': 'NoSession',
            'message': 'No active GnuCash session; the book failed to '
            're-open. Retry POST /revert to recover.', 'data': ''}]}),
            status=503, mimetype='application/json')

@app.route('/accounts', methods=['GET', 'POST'])
def api_accounts():

    if request.method == 'GET':

        accounts = getAccounts(session.book)

        return Response(json.dumps(accounts), mimetype='application/json')

    elif request.method == 'POST':

        name = str(request.form.get('name', ''))
        currency = str(request.form.get('currency', ''))
        account_type_id = request.form.get('account_type_id', '')
        parent_account_guid = str(request.form.get('parent_account_guid', ''))
        description = str(request.form.get('description', ''))
        code = str(request.form.get('code', ''))

        try:
            account = addAccount(session.book, name, currency,
                account_type_id, parent_account_guid, description, code)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(account), status=201,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/accounts/<guid>', methods=['GET'])
def api_account(guid):

    account = getAccount(session.book, guid)
    
    if account is None:
        abort(404)
    else:
        return Response(json.dumps(account), mimetype='application/json')

@app.route('/accounts/<guid>/splits', methods=['GET'])
def api_account_splits(guid):

    date_posted_from = request.args.get('date_posted_from', None)
    date_posted_to = request.args.get('date_posted_to', None)

    # Pagination. limit defaults to 100 and is capped at 500; offset defaults
    # to 0. Invalid values fall back to the defaults rather than erroring.
    try:
        limit = int(request.args.get('limit', 100))
    except ValueError:
        limit = 100
    if limit < 0:
        limit = 100
    if limit > 500:
        limit = 500

    try:
        offset = int(request.args.get('offset', 0))
    except ValueError:
        offset = 0
    if offset < 0:
        offset = 0

    # Optional comma-separated list controlling which related objects are
    # embedded in each split. Defaults to the historic behaviour of including
    # the parent transaction and the other split.
    include_arg = request.args.get('include', None)
    if include_arg is None:
        include = set(['transaction', 'other_split'])
    else:
        include = set(part for part in include_arg.split(',') if part != '')

    # check account exists
    account = getAccount(session.book, guid)

    if account is None:
        abort(404)

    # The queried account is the same for every split in the result, so build a
    # shallow account dict once (from the dict getAccount already computed) and
    # reuse it for every row instead of re-serialising the account each time.
    account_dict = dict((key, account[key]) for key in
        ('guid', 'name', 'type_id', 'description', 'currency')
        if key in account)

    splits, total = getAccountSplits(session.book, guid, date_posted_from,
        date_posted_to, limit, offset, include, account_dict)

    return Response(json.dumps({'splits': splits, 'total': total,
        'limit': limit, 'offset': offset}), mimetype='application/json')


@app.route('/accounts/<guid>/splits/search', methods=['GET'])
def api_account_splits_search(guid):

    # Optional filters: description (free-text), date_posted_from/to and
    # amount_from/to ranges. Any combination may be supplied and they are
    # AND-combined; an absent or empty value means "no constraint".
    description = request.args.get('description', None) or None
    date_posted_from = request.args.get('date_posted_from', None) or None
    date_posted_to = request.args.get('date_posted_to', None) or None
    amount_from = request.args.get('amount_from', None) or None
    amount_to = request.args.get('amount_to', None) or None

    # Search this account only, or this account and all of its descendants.
    include_children = request.args.get('include_children', '').lower() in (
        'true', '1', 'yes')

    # Pagination. limit defaults to 100 and is capped at 500; offset defaults
    # to 0. Invalid values fall back to the defaults rather than erroring.
    try:
        limit = int(request.args.get('limit', 100))
    except ValueError:
        limit = 100
    if limit < 0:
        limit = 100
    if limit > 500:
        limit = 500

    try:
        offset = int(request.args.get('offset', 0))
    except ValueError:
        offset = 0
    if offset < 0:
        offset = 0

    # Optional comma-separated list controlling which related objects are
    # embedded in each split. Defaults to including the parent transaction and
    # the other split, matching the plain splits endpoint.
    include_arg = request.args.get('include', None)
    if include_arg is None:
        include = set(['transaction', 'other_split'])
    else:
        include = set(part for part in include_arg.split(',') if part != '')

    # check account exists
    account = getAccount(session.book, guid)

    if account is None:
        abort(404)

    # When searching across child accounts each result may belong to a
    # different account, so let splitToDict serialise each split's own account.
    # For a single-account search every row shares the queried account, so build
    # that shallow dict once and reuse it for every row.
    if include_children:
        account_dict = None
    else:
        account_dict = dict((key, account[key]) for key in
            ('guid', 'name', 'type_id', 'description', 'currency')
            if key in account)

    # The query helper parses the date strings and amount values; surface
    # malformed input as a 400 rather than letting it become a 500.
    try:
        splits, total = getAccountSplits(session.book, guid, date_posted_from,
            date_posted_to, limit, offset, include, account_dict, description,
            amount_from, amount_to, include_children)
    except (ValueError, ArithmeticError):
        return Response(json.dumps({'errors': [{'type': 'InvalidSearch',
            'message': 'date_posted_from/to must be formatted YYYY-MM-DD and '
            'amount_from/to must be numeric', 'data': None}]}), status=400,
            mimetype='application/json')

    return Response(json.dumps({'splits': splits, 'total': total,
        'limit': limit, 'offset': offset}), mimetype='application/json')


@app.route('/transactions', methods=['POST'])
def api_transactions():

    if request.method == 'POST':
        
        currency = str(request.form.get('currency', ''))
        description = str(request.form.get('description', ''))
        num = str(request.form.get('num', ''))
        date_posted = str(request.form.get('date_posted', ''))

        splitvalue1 = int(request.form.get('splitvalue1', ''))
        splitaccount1 = str(request.form.get('splitaccount1', ''))
        splitvalue2 = int(request.form.get('splitvalue2', ''))
        splitaccount2 = str(request.form.get('splitaccount2', ''))

        splits = [
            {'value': splitvalue1, 'account_guid': splitaccount1},
            {'value': splitvalue2, 'account_guid': splitaccount2}]

        try:
            transaction = addTransaction(session.book, num, description,
                date_posted, currency, splits)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(transaction), status=201,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/transactions/<guid>', methods=['GET', 'POST', 'DELETE'])
def api_transaction(guid):

    if request.method == 'GET':

        transaction = getTransaction(session.book, guid)

        if transaction is None:
            abort(404)
        
        return Response(json.dumps(transaction), mimetype='application/json')

    elif request.method == 'POST':

        currency = str(request.form.get('currency', ''))
        description = str(request.form.get('description', ''))
        num = str(request.form.get('num', ''))
        date_posted = str(request.form.get('date_posted', ''))

        splitguid1 = str(request.form.get('splitguid1', ''))
        splitvalue1 = int(request.form.get('splitvalue1', ''))
        splitaccount1 = str(request.form.get('splitaccount1', ''))
        splitguid2 = str(request.form.get('splitguid2', ''))
        splitvalue2 = int(request.form.get('splitvalue2', ''))
        splitaccount2 = str(request.form.get('splitaccount2', ''))

        splits = [
            {'guid': splitguid1,
            'value': splitvalue1,
            'account_guid': splitaccount1},
            {'guid': splitguid2,
            'value': splitvalue2,
            'account_guid': splitaccount2}
        ]

        try:
            transaction = editTransaction(session.book, guid, num, description,
                date_posted, currency, splits)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400, mimetype='application/json')
        else:
            return Response(json.dumps(transaction), status=200,
                mimetype='application/json')

    elif request.method == 'DELETE':

        deleteTransaction(session.book, guid)

        return Response('', status=200, mimetype='application/json')

    else:
        abort(405)

@app.route('/bills', methods=['GET', 'POST'])
def api_bills():

    if request.method == 'GET':
        
        is_paid = request.args.get('is_paid', None)
        is_active = request.args.get('is_active', None)
        date_opened_to = request.args.get('date_opened_to', None)
        date_opened_from = request.args.get('date_opened_from', None)

        if is_paid == '1':
            is_paid = 1
        elif is_paid == '0':
            is_paid = 0
        else:
            is_paid = None

        if is_active == '1':
            is_active = 1
        elif is_active == '0':
            is_active = 0
        else:
            is_active = None

        bills = getBills(session.book, None, is_paid, is_active,
            date_opened_from, date_opened_to)

        return Response(json.dumps(bills), mimetype='application/json')

    elif request.method == 'POST':

        id = str(request.form.get('id', None))

        if id == '':
            id = None
        elif id != None:
            id = str(id)

        vendor_id = str(request.form.get('vendor_id', ''))
        currency = str(request.form.get('currency', ''))
        date_opened = str(request.form.get('date_opened', ''))
        notes = str(request.form.get('notes', ''))

        try:
            bill = addBill(session.book, id, vendor_id, currency, date_opened,
                notes)
        except Error as error:
            # handle incorrect parameter errors
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400, mimetype='application/json')
        else:
            return Response(json.dumps(bill), status=201,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/bills/<id>', methods=['GET', 'POST', 'PAY'])
def api_bill(id):

    if request.method == 'GET':

        bill = getBill(session.book, id)
        
        if bill is None:
            abort(404)
        else:
            return Response(json.dumps(bill), mimetype='application/json')

    elif request.method == 'POST':

        vendor_id = str(request.form.get('vendor_id', ''))
        currency = str(request.form.get('currency', ''))
        date_opened = request.form.get('date_opened', None)
        notes = str(request.form.get('notes', ''))
        posted = request.form.get('posted', None)
        posted_account_guid = str(request.form.get('posted_account_guid', ''))
        posted_date = request.form.get('posted_date', '')
        due_date = request.form.get('due_date', '')
        posted_memo = str(request.form.get('posted_memo', ''))
        posted_accumulatesplits = request.form.get('posted_accumulatesplits',
            '')
        posted_autopay = request.form.get('posted_autopay', '')

        if posted == '1':
            posted = 1
        else:
            posted = 0

        if (posted_accumulatesplits == '1'
            or posted_accumulatesplits == 'true'
            or posted_accumulatesplits == 'True'
            or posted_accumulatesplits == True):
            posted_accumulatesplits = True
        else:
            posted_accumulatesplits = False

        if posted_autopay == '1':
            posted_autopay = True
        else:
            posted_autopay = False
        try:
            bill = updateBill(session.book, id, vendor_id, currency,
                date_opened, notes, posted, posted_account_guid, posted_date,
                due_date, posted_memo, posted_accumulatesplits, posted_autopay)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(bill), status=200,
                mimetype='application/json')

        if bill is None:
            abort(404)
        else:
            return Response(json.dumps(bill),
                mimetype='application/json')

    elif request.method == 'PAY':
        
        posted_account_guid = str(request.form.get('posted_account_guid', ''))
        transfer_account_guid = str(request.form.get('transfer_account_guid',
            ''))
        payment_date = request.form.get('payment_date', '')
        num = str(request.form.get('num', ''))
        memo = str(request.form.get('posted_memo', ''))
        auto_pay = request.form.get('auto_pay', '')

        try:
            bill = payBill(session.book, id, posted_account_guid,
                transfer_account_guid, payment_date, memo, num, auto_pay)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
            mimetype='application/json')
        else:
            return Response(json.dumps(bill), status=200,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/bills/<id>/entries', methods=['GET', 'POST'])
def api_bill_entries(id):

    bill = getBill(session.book, id)
    
    if bill is None:
        abort(404)
    else:
        if request.method == 'GET':
            return Response(json.dumps(bill['entries']), mimetype='application/json')
        elif request.method == 'POST':

            date = str(request.form.get('date', ''))
            description = str(request.form.get('description', ''))
            account_guid = str(request.form.get('account_guid', ''))
            quantity = str(request.form.get('quantity', ''))
            price = str(request.form.get('price', ''))

            try:
                entry = addBillEntry(session.book, id, date, description,
                    account_guid, quantity, price)
            except Error as error:
                return Response(json.dumps({'errors': [{'type' : error.type,
                    'message': error.message, 'data': error.data}]}),
                    status=400, mimetype='application/json')
            else:
                return Response(json.dumps(entry), status=201,
                    mimetype='application/json')

        else:
            abort(405)

@app.route('/invoices', methods=['GET', 'POST'])
def api_invoices():

    if request.method == 'GET':
        
        is_paid = request.args.get('is_paid', None)
        is_active = request.args.get('is_active', None)
        date_due_to = request.args.get('date_due_to', None)
        date_due_from = request.args.get('date_due_from', None)

        if is_paid == '1':
            is_paid = 1
        elif is_paid == '0':
            is_paid = 0
        else:
            is_paid = None

        if is_active == '1':
            is_active = 1
        elif is_active == '0':
            is_active = 0
        else:
            is_active = None

        invoices = getInvoices(session.book, None, is_paid, is_active,
            date_due_from, date_due_to)

        return Response(json.dumps(invoices), mimetype='application/json')

    elif request.method == 'POST':

        id = str(request.form.get('id', None))

        if id == '':
            id = None
        elif id != None:
            id = str(id)

        customer_id = str(request.form.get('customer_id', ''))
        currency = str(request.form.get('currency', ''))
        date_opened = str(request.form.get('date_opened', ''))
        notes = str(request.form.get('notes', ''))

        try:
            invoice = addInvoice(session.book, id, customer_id, currency,
                date_opened, notes)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(invoice), status=201,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/invoices/<id>', methods=['GET', 'POST', 'PAY'])
def api_invoice(id):

    if request.method == 'GET':

        invoice = getInvoice(session.book, id)
        
        if invoice is None:
            abort(404)
        else:
            return Response(json.dumps(invoice), mimetype='application/json')

    elif request.method == 'POST':

        customer_id = str(request.form.get('customer_id', ''))
        currency = str(request.form.get('currency', ''))
        date_opened = request.form.get('date_opened', None)
        notes = str(request.form.get('notes', ''))
        posted = request.form.get('posted', None)
        posted_account_guid = str(request.form.get('posted_account_guid', ''))
        posted_date = request.form.get('posted_date', '')
        due_date = request.form.get('due_date', '')
        posted_memo = str(request.form.get('posted_memo', ''))
        posted_accumulatesplits = request.form.get('posted_accumulatesplits',
            '')
        posted_autopay = request.form.get('posted_autopay', '')

        if posted == '1':
            posted = 1
        else:
            posted = 0

        if (posted_accumulatesplits == '1'
            or posted_accumulatesplits == 'true'
            or posted_accumulatesplits == 'True'
            or posted_accumulatesplits == True):
            posted_accumulatesplits = True
        else:
            posted_accumulatesplits = False

        if posted_autopay == '1':
            posted_autopay = True
        else:
            posted_autopay = False
        try:
            invoice = updateInvoice(session.book, id, customer_id, currency,
                date_opened, notes, posted, posted_account_guid, posted_date,
                due_date, posted_memo, posted_accumulatesplits, posted_autopay)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(invoice), status=200,
                mimetype='application/json')

        if invoice is None:
            abort(404)
        else:
            return Response(json.dumps(invoice), mimetype='application/json')

    elif request.method == 'PAY':
        
        posted_account_guid = str(request.form.get('posted_account_guid', ''))
        transfer_account_guid = str(request.form.get('transfer_account_guid',
            ''))
        payment_date = request.form.get('payment_date', '')
        num = str(request.form.get('num', ''))
        memo = str(request.form.get('posted_memo', ''))
        auto_pay = request.form.get('auto_pay', '')

        try:
            invoice = payInvoice(session.book, id, posted_account_guid,
                transfer_account_guid, payment_date, memo, num, auto_pay)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
            mimetype='application/json')
        else:
            return Response(json.dumps(invoice), status=200,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/invoices/<id>/entries', methods=['GET', 'POST'])
def api_invoice_entries(id):

    invoice = getInvoice(session.book, id)
    
    if invoice is None:
        abort(404)
    else:
        if request.method == 'GET':
            return Response(json.dumps(invoice['entries']),
                mimetype='application/json')
        elif request.method == 'POST':

            date = str(request.form.get('date', ''))
            description = str(request.form.get('description', ''))
            account_guid = str(request.form.get('account_guid', ''))
            quantity = str(request.form.get('quantity', ''))
            price = str(request.form.get('price', ''))

            try:
                entry = addEntry(session.book, id, date, description,
                    account_guid, quantity, price)
            except Error as error:
                return Response(json.dumps({'errors': [{'type' : error.type,
                    'message': error.message, 'data': error.data}]}),
                    status=400, mimetype='application/json')
            else:
                return Response(json.dumps(entry), status=201,
                    mimetype='application/json')

        else:
            abort(405)

@app.route('/entries/<guid>', methods=['GET', 'POST', 'DELETE'])
def api_entry(guid):

    entry = getEntry(session.book, guid)
    
    if entry is None:
        abort(404)
    else:
        if request.method == 'GET':
            return Response(json.dumps(entry), mimetype='application/json')
        elif request.method == 'POST':

            date = str(request.form.get('date', ''))
            description = str(request.form.get('description', ''))
            account_guid = str(request.form.get('account_guid', ''))
            quantity = str(request.form.get('quantity', ''))
            price = str(request.form.get('price', ''))

            try:
                entry = updateEntry(session.book, guid, date, description,
                    account_guid, quantity, price)
            except Error as error:
                return Response(json.dumps({'errors': [{'type' : error.type,
                    'message': error.message, 'data': error.data}]}),
                    status=400, mimetype='application/json')
            else:
                return Response(json.dumps(entry), status=200,
                    mimetype='application/json')

        elif request.method == 'DELETE':

            deleteEntry(session.book, guid)

            return Response('', status=201, mimetype='application/json')

        else:
            abort(405)

@app.route('/customers', methods=['GET', 'POST'])
def api_customers(): 

    if request.method == 'GET':
        customers = getCustomers(session.book)
        return Response(json.dumps(customers), mimetype='application/json')
    elif request.method == 'POST':

        id = str(request.form.get('id', None))

        if id == '':
            id = None
        elif id != None:
            id = str(id)

        currency = str(request.form.get('currency', ''))
        name = str(request.form.get('name', ''))
        contact = str(request.form.get('contact', ''))
        address_line_1 = str(request.form.get('address_line_1', ''))
        address_line_2 = str(request.form.get('address_line_2', ''))
        address_line_3 = str(request.form.get('address_line_3', ''))
        address_line_4 = str(request.form.get('address_line_4', ''))
        phone = str(request.form.get('phone', ''))
        fax = str(request.form.get('fax', ''))
        email = str(request.form.get('email', ''))

        try:
            customer = addCustomer(session.book, id, currency, name, contact,
                address_line_1, address_line_2, address_line_3, address_line_4,
                phone, fax, email)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(customer), status=201,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/customers/<id>', methods=['GET', 'POST'])
def api_customer(id):

    if request.method == 'GET':

        customer = getCustomer(session.book, id)

        if customer is None:
            abort(404)
        else:
            return Response(json.dumps(customer), mimetype='application/json')

    elif request.method == 'POST':

        id = str(request.form.get('id', None))

        name = str(request.form.get('name', ''))
        contact = str(request.form.get('contact', ''))
        address_line_1 = str(request.form.get('address_line_1', ''))
        address_line_2 = str(request.form.get('address_line_2', ''))
        address_line_3 = str(request.form.get('address_line_3', ''))
        address_line_4 = str(request.form.get('address_line_4', ''))
        phone = str(request.form.get('phone', ''))
        fax = str(request.form.get('fax', ''))
        email = str(request.form.get('email', ''))

        try:
            customer = updateCustomer(session.book, id, name, contact,
                address_line_1, address_line_2, address_line_3, address_line_4,
                phone, fax, email)
        except Error as error:
            if error.type == 'NoCustomer':
                return Response(json.dumps({'errors': [{'type' : error.type,
                    'message': error.message, 'data': error.data}]}),
                    status=404, mimetype='application/json')
            else:
                return Response(json.dumps({'errors': [{'type' : error.type,
                    'message': error.message, 'data': error.data}]}),
                    status=400, mimetype='application/json')
        else:
            return Response(json.dumps(customer), status=200,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/customers/<id>/invoices', methods=['GET'])
def api_customer_invoices(id):

    customer = getCustomer(session.book, id)
    
    if customer is None:
        abort(404)
    
    invoices = getInvoices(session.book, customer['guid'], None, None, None,
        None)
    
    return Response(json.dumps(invoices), mimetype='application/json')

@app.route('/vendors', methods=['GET', 'POST'])
def api_vendors(): 

    if request.method == 'GET':
        vendors = getVendors(session.book)
        return Response(json.dumps(vendors), mimetype='application/json')
    elif request.method == 'POST':

        id = str(request.form.get('id', None))

        if id == '':
            id = None
        elif id != None:
            id = str(id)

        currency = str(request.form.get('currency', ''))
        name = str(request.form.get('name', ''))
        contact = str(request.form.get('contact', ''))
        address_line_1 = str(request.form.get('address_line_1', ''))
        address_line_2 = str(request.form.get('address_line_2', ''))
        address_line_3 = str(request.form.get('address_line_3', ''))
        address_line_4 = str(request.form.get('address_line_4', ''))
        phone = str(request.form.get('phone', ''))
        fax = str(request.form.get('fax', ''))
        email = str(request.form.get('email', ''))

        try:
            vendor = addVendor(session.book, id, currency, name, contact,
                address_line_1, address_line_2, address_line_3, address_line_4,
                phone, fax, email)
        except Error as error:
            return Response(json.dumps({'errors': [{'type' : error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(vendor), status=201,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/vendors/<id>', methods=['GET', 'POST'])
def api_vendor(id):

    if request.method == 'GET':

        vendor = getVendor(session.book, id)

        if vendor is None:
            abort(404)
        else:
            return Response(json.dumps(vendor), mimetype='application/json')
    else:
        abort(405)

@app.route('/vendors/<id>/bills', methods=['GET'])
def api_vendor_bills(id):

    vendor = getVendor(session.book, id)

    if vendor is None:
        abort(404)

    bills = getBills(session.book, vendor['guid'], None, None, None, None)

    return Response(json.dumps(bills), mimetype='application/json')

@app.route('/commodities', methods=['GET', 'POST'])
def api_commodities():

    if request.method == 'GET':
        namespace = request.args.get('namespace', None)
        commodities = getCommodities(session.book, namespace)
        return Response(json.dumps(commodities), mimetype='application/json')

    elif request.method == 'POST':
        namespace = str(request.form.get('namespace', ''))
        mnemonic = str(request.form.get('mnemonic', ''))
        fullname = str(request.form.get('fullname', ''))
        cusip = str(request.form.get('cusip', ''))
        fraction = request.form.get('fraction', '10000')
        quote_source = str(request.form.get('quote_source', ''))
        quote_tz = str(request.form.get('quote_tz', ''))

        try:
            commodity = addCommodity(session.book, namespace, mnemonic,
                fullname, cusip, fraction, quote_source, quote_tz)
        except Error as error:
            return Response(json.dumps({'errors': [{'type': error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(commodity), status=201,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/commodities/<namespace>/<mnemonic>', methods=['GET'])
def api_commodity(namespace, mnemonic):

    commodity = getCommodity(session.book, namespace, mnemonic)

    if commodity is None:
        abort(404)

    return Response(json.dumps(commodity), mimetype='application/json')

@app.route('/commodities/<namespace>/<mnemonic>/prices/latest',
    methods=['GET'])
def api_commodity_price_latest(namespace, mnemonic):

    currency_mnemonic = str(request.args.get('currency_mnemonic', ''))

    try:
        price = getLatestPrice(session.book, namespace, mnemonic,
            currency_mnemonic)
    except Error as error:
        return Response(json.dumps({'errors': [{'type': error.type,
            'message': error.message, 'data': error.data}]}), status=400,
            mimetype='application/json')

    if price is None:
        abort(404)

    return Response(json.dumps(price), mimetype='application/json')

@app.route('/commodities/<namespace>/<mnemonic>/prices/nearest',
    methods=['GET'])
def api_commodity_price_nearest(namespace, mnemonic):

    currency_mnemonic = str(request.args.get('currency_mnemonic', ''))
    date = str(request.args.get('date', ''))

    try:
        price = getNearestPrice(session.book, namespace, mnemonic,
            currency_mnemonic, date)
    except Error as error:
        return Response(json.dumps({'errors': [{'type': error.type,
            'message': error.message, 'data': error.data}]}), status=400,
            mimetype='application/json')

    if price is None:
        abort(404)

    return Response(json.dumps(price), mimetype='application/json')

@app.route('/prices', methods=['GET', 'POST'])
def api_prices():

    if request.method == 'GET':
        commodity_namespace = request.args.get('commodity_namespace', None)
        commodity_mnemonic = request.args.get('commodity_mnemonic', None)
        currency_mnemonic = request.args.get('currency_mnemonic', None)
        date_from = request.args.get('date_from', None)
        date_to = request.args.get('date_to', None)

        try:
            prices = getPrices(session.book, commodity_namespace,
                commodity_mnemonic, currency_mnemonic, date_from, date_to)
        except Error as error:
            return Response(json.dumps({'errors': [{'type': error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')

        return Response(json.dumps(prices), mimetype='application/json')

    elif request.method == 'POST':
        commodity_namespace = str(request.form.get('commodity_namespace', ''))
        commodity_mnemonic = str(request.form.get('commodity_mnemonic', ''))
        currency_mnemonic = str(request.form.get('currency_mnemonic', ''))
        value = str(request.form.get('value', ''))
        value_num = request.form.get('value_num', None)
        value_denom = request.form.get('value_denom', None)
        date = str(request.form.get('date', ''))
        source = str(request.form.get('source', ''))
        price_type = str(request.form.get('type', ''))

        try:
            price = addPrice(session.book, commodity_namespace,
                commodity_mnemonic, currency_mnemonic, value, value_num,
                value_denom, date, source, price_type)
        except Error as error:
            return Response(json.dumps({'errors': [{'type': error.type,
                'message': error.message, 'data': error.data}]}), status=400,
                mimetype='application/json')
        else:
            return Response(json.dumps(price), status=201,
                mimetype='application/json')

    else:
        abort(405)

@app.route('/prices/<guid>', methods=['GET', 'DELETE'])
def api_price(guid):

    if request.method == 'GET':
        price = getPrice(session.book, guid)
        if price is None:
            abort(404)
        return Response(json.dumps(price), mimetype='application/json')

    elif request.method == 'DELETE':
        if not deletePrice(session.book, guid):
            abort(404)
        return Response('', status=204, mimetype='application/json')

    else:
        abort(405)

@app.route('/save', methods=['POST'])
def api_save():

    # Flush all in-memory changes to the backend. Without this the book is
    # only persisted when the server shuts down cleanly (see shutdown()).
    try:
        session.save()
    except gnucash.GnuCashBackendException as error:
        return Response(json.dumps({'errors': [{'type':
            'GnuCashBackendException', 'message':
            'Failed to save the book to the backend.',
            'data': str(error)}]}), status=500,
            mimetype='application/json')

    return Response('', status=204, mimetype='application/json')

@app.route('/revert', methods=['POST'])
def api_revert():

    # Discard all unsaved in-memory changes by ending the current session
    # without saving and re-opening it, which re-reads the book from the
    # backend. There is no native in-place revert in qof (re-loading a session
    # requires an empty book), so the session is recreated -- the same approach
    # the GnuCash GUI takes in gnc_file_revert().
    global session

    # The old session must be torn down before a new one can lock the backend,
    # so there is an unavoidable window with no usable session. Drop the global
    # reference up front so a failure part-way through never leaves `session`
    # pointing at a half-destroyed object.
    old_session, session = session, None

    if old_session is not None:
        try:
            old_session.end()
            old_session.destroy()
        except gnucash.GnuCashBackendException:
            # Best effort -- the backend is going away regardless. Fall through
            # and still try to re-open a fresh session below.
            pass

    # Re-open to re-read the book. If this fails, `session` stays None and the
    # require_session guard turns every other route into a clean 503 until a
    # later /revert succeeds.
    try:
        session = gnucash.Session(connection_string,
            SessionOpenMode.SESSION_BREAK_LOCK)
    except gnucash.GnuCashBackendException as error:
        return Response(json.dumps({'errors': [{'type':
            'GnuCashBackendException', 'message':
            'Reverted in-memory changes but failed to re-open the book; the '
            'server has no active session until POST /revert succeeds.',
            'data': str(error)}]}), status=503,
            mimetype='application/json')

    return Response('', status=204, mimetype='application/json')

@app.route('/session', methods=['GET'])
def api_session():

    # Whether the book has in-memory changes not yet flushed to the backend
    # (i.e. a POST /save would persist something). Sourced from qof's own
    # predicate, exposed on Book as session_not_saved() by
    # add_constructor_and_methods_with_prefix('qof_book_', ...) in gnucash_core.py
    # (see also example_scripts/simple_book.py). Lets a client show unsaved
    # state and offer Save/Revert.
    return Response(json.dumps({'dirty': session.book.session_not_saved()}),
        status=200, mimetype='application/json')

def getCustomers(book):

    query = gnucash.Query()
    query.search_for('gncCustomer')
    query.set_book(book)
    customers = []

    for result in query.run():
        customers.append(gnucash_simple.customerToDict(
            gnucash.gnucash_business.Customer(instance=result)))

    query.destroy()

    return customers

def getCustomer(book, id):

    customer = book.CustomerLookupByID(id)

    if customer is None:
        return None
    else:
        return gnucash_simple.customerToDict(customer)

def getVendors(book):

    query = gnucash.Query()
    query.search_for('gncVendor')
    query.set_book(book)
    vendors = []

    for result in query.run():
        vendors.append(gnucash_simple.vendorToDict(
            gnucash.gnucash_business.Vendor(instance=result)))

    query.destroy()

    return vendors

def getVendor(book, id):

    vendor = book.VendorLookupByID(id)

    if vendor is None:
        return None
    else:
        return gnucash_simple.vendorToDict(vendor)

def getAccounts(book):

    accounts = gnucash_simple.accountToDict(book.get_root_account())

    return accounts

def getAccountsFlat(book):

    accounts = gnucash_simple.accountToDict(book.get_root_account())

    flat_accounts = getSubAccounts(accounts)

    for n, account in enumerate(flat_accounts):
        account.pop('subaccounts')

    filtered_flat_account = []

    type_ids = [9]

    for n, account in enumerate(flat_accounts):
        if account['type_id'] in type_ids:
            filtered_flat_account.append(account)
            print(account['name'] + ' ' + str(account['type_id']))

    return filtered_flat_account

def getSubAccounts(account):

    flat_accounts = []

    if 'subaccounts' in list(account.keys()):
        for n, subaccount in enumerate(account['subaccounts']):
            flat_accounts.append(subaccount)
            flat_accounts = flat_accounts + getSubAccounts(subaccount)

    return flat_accounts

def getAccount(book, guid):

    account_guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(guid, account_guid)

    account = account_guid.AccountLookup(book)

    if account is None:
        return None

    account = gnucash_simple.accountToDict(account)

    if account is None:
        return None
    else:
        return account


def getTransaction(book, guid):

    transaction_guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(guid, transaction_guid)

    transaction = transaction_guid.TransactionLookup(book)

    if transaction is None:
        return None

    transaction = gnucash_simple.transactionToDict(transaction, ['splits'])

    if transaction is None:
        return None
    else:
        return transaction

def getTransactions(book, account_guid, date_posted_from, date_posted_to):

    query = gnucash.Query()

    query.search_for('Trans')
    query.set_book(book)

    transactions = []

    for transaction in query.run():
        transactions.append(gnucash_simple.transactionToDict(
            gnucash.gnucash_business.Transaction(instance=transaction)))

    query.destroy()

    return transactions

def getAccountSplits(book, guid, date_posted_from, date_posted_to, limit,
    offset, include, account_dict, description=None, amount_from=None,
    amount_to=None, include_children=False):

    SPLIT_TRANS = 'trans'
    TRANS_DATE_POSTED = 'date-posted'
    TRANS_DESCRIPTION = 'desc'
    SPLIT_ACCOUNT = 'account'
    SPLIT_VALUE = 'amount'
    QOF_PARAM_GUID = 'guid'

    account_guid = gnucash.gnucash_core.GUID()
    gnucash.gnucash_core.GUIDString(guid, account_guid)

    account = account_guid.AccountLookup(book)
    if account is None:
        return [], 0

    query = gnucash.Query()
    query.search_for('Split')
    query.set_book(book)

    # The set of accounts the search covers: just this account, or this account
    # plus every descendant when include_children is requested.
    if include_children:
        accounts = [account] + account.get_descendants()
    else:
        accounts = [account]

    # Add the account match FIRST, before the date/description/amount filters.
    # qof stores a query in disjunctive normal form (an OR of AND-groups): a
    # term added with QOF_QUERY_AND is cross-producted into every existing
    # AND-group, while QOF_QUERY_OR starts a new group. Building the accounts as
    # an OR-group up front and then AND-ing the filters distributes each filter
    # across every account, yielding (acctA OR acctB OR ...) AND date AND desc
    # AND amount. (Doing it the other way round would leave the later accounts
    # OR-ed in unfiltered.) The multi-account add_guid_list_match isn't usable
    # from Python -- there's no GList input typemap -- so we OR together single
    # add_guid_match terms, which is the same binding the single-account path
    # has always used. For a single account this is exactly one AND term, so the
    # query is identical to before.
    for index, search_account in enumerate(accounts):
        search_account_guid = gnucash.gnucash_core.GUID()
        gnucash.gnucash_core.GUIDString(
            search_account.GetGUID().to_string(), search_account_guid)
        op = QOF_QUERY_AND if index == 0 else QOF_QUERY_OR
        query.add_guid_match(
            [SPLIT_ACCOUNT, QOF_PARAM_GUID], search_account_guid, op)

    if date_posted_from != None:
        pred_data = gnucash.gnucash_core.QueryDatePredicate(
            QOF_COMPARE_GTE, QOF_DATE_MATCH_NORMAL, datetime.datetime.strptime(
                date_posted_from, "%Y-%m-%d").date())
        param_list = [SPLIT_TRANS, TRANS_DATE_POSTED]
        query.add_term(param_list, pred_data, QOF_QUERY_AND)

    if date_posted_to != None:
        pred_data = gnucash.gnucash_core.QueryDatePredicate(
            QOF_COMPARE_LTE, QOF_DATE_MATCH_NORMAL, datetime.datetime.strptime(
                date_posted_to, "%Y-%m-%d").date())
        param_list = [SPLIT_TRANS, TRANS_DATE_POSTED]
        query.add_term(param_list, pred_data, QOF_QUERY_AND)

    # Free-text, case-insensitive substring match on the parent transaction's
    # description.
    if description != None:
        pred_data = gnucash.gnucash_core.QueryStringPredicate(
            QOF_COMPARE_CONTAINS, description,
            QOF_STRING_MATCH_CASEINSENSITIVE, False)
        param_list = [SPLIT_TRANS, TRANS_DESCRIPTION]
        query.add_term(param_list, pred_data, QOF_QUERY_AND)

    # Amount bounds. The numeric predicate compares the absolute value of the
    # split amount, so the range matches by magnitude regardless of whether the
    # split is a debit or a credit.
    if amount_from != None:
        pred_data = gnucash.gnucash_core.QueryNumericPredicate(
            QOF_COMPARE_GTE, QOF_NUMERIC_MATCH_ANY,
            gnc_numeric_from_decimal(Decimal(amount_from)))
        query.add_term([SPLIT_VALUE], pred_data, QOF_QUERY_AND)

    if amount_to != None:
        pred_data = gnucash.gnucash_core.QueryNumericPredicate(
            QOF_COMPARE_LTE, QOF_NUMERIC_MATCH_ANY,
            gnc_numeric_from_decimal(Decimal(amount_to)))
        query.add_term([SPLIT_VALUE], pred_data, QOF_QUERY_AND)

    # Sort by the transaction's posted date, with the split's own guid as a
    # stable tiebreaker, so that limit/offset paging returns consistent,
    # non-overlapping pages. The Query class doesn't bind these so we call the
    # underlying qof_query functions on the raw query pointer. An empty list
    # marshals to NULL, i.e. no tertiary sort key.
    gnucash_core_c.qof_query_set_sort_order(
        query.instance, [SPLIT_TRANS, TRANS_DATE_POSTED], [QOF_PARAM_GUID], [])
    gnucash_core_c.qof_query_set_sort_increasing(
        query.instance, True, True, True)

    # query.run() materialises split pointers cheaply; the expense is the
    # per-split serialisation below, so slice to the requested page first and
    # only serialise that window.
    results = query.run()
    total = len(results)

    entities = ['account']
    if 'transaction' in include:
        entities.append('transaction')
    if 'other_split' in include:
        entities.append('other_split')

    splits = []

    for split in results[offset:offset + limit]:
        splits.append(gnucash_simple.splitToDict(
            gnucash.gnucash_business.Split(instance=split),
            entities, account_dict))

    query.destroy()

    return splits, total

def getInvoices(book, customer, is_paid, is_active, date_due_from,
    date_due_to):

    query = gnucash.Query()
    query.search_for('gncInvoice')
    query.set_book(book)

    if is_paid == 0:
        query.add_boolean_match([INVOICE_IS_PAID], False, QOF_QUERY_AND)
    elif is_paid == 1:
        query.add_boolean_match([INVOICE_IS_PAID], True, QOF_QUERY_AND)

    # active = JOB_IS_ACTIVE
    if is_active == 0:
        query.add_boolean_match(['active'], False, QOF_QUERY_AND)
    elif is_active == 1:
        query.add_boolean_match(['active'], True, QOF_QUERY_AND)

    QOF_PARAM_GUID = 'guid'
    INVOICE_OWNER = 'owner'

    if customer != None:
        customer_guid = gnucash.gnucash_core.GUID() 
        gnucash.gnucash_core.GUIDString(customer, customer_guid)
        query.add_guid_match(
            [INVOICE_OWNER, QOF_PARAM_GUID], customer_guid, QOF_QUERY_AND)

    if date_due_from != None:
        pred_data = gnucash.gnucash_core.QueryDatePredicate(
            QOF_COMPARE_GTE, 2, datetime.datetime.strptime(
                date_due_from, "%Y-%m-%d").date())
        query.add_term(['date_due'], pred_data, QOF_QUERY_AND)

    if date_due_to != None:
        pred_data = gnucash.gnucash_core.QueryDatePredicate(
            QOF_COMPARE_LTE, 2, datetime.datetime.strptime(
                date_due_to, "%Y-%m-%d").date())
        query.add_term(['date_due'], pred_data, QOF_QUERY_AND)

    # return only invoices (1 = invoices)
    pred_data = gnucash.gnucash_core.QueryInt32Predicate(QOF_COMPARE_EQUAL, 1)
    query.add_term([INVOICE_TYPE], pred_data, QOF_QUERY_AND)

    invoices = []

    for result in query.run():
        invoices.append(gnucash_simple.invoiceToDict(
            gnucash.gnucash_business.Invoice(instance=result)))

    query.destroy()

    return invoices

def getBills(book, customer, is_paid, is_active, date_opened_from,
    date_opened_to):

    query = gnucash.Query()
    query.search_for('gncInvoice')
    query.set_book(book)

    if is_paid == 0:
        query.add_boolean_match([INVOICE_IS_PAID], False, QOF_QUERY_AND)
    elif is_paid == 1:
        query.add_boolean_match([INVOICE_IS_PAID], True, QOF_QUERY_AND)

    # active = JOB_IS_ACTIVE
    if is_active == 0:
        query.add_boolean_match(['active'], False, QOF_QUERY_AND)
    elif is_active == 1:
        query.add_boolean_match(['active'], True, QOF_QUERY_AND)

    QOF_PARAM_GUID = 'guid'
    INVOICE_OWNER = 'owner'

    if customer != None:
        customer_guid = gnucash.gnucash_core.GUID() 
        gnucash.gnucash_core.GUIDString(customer, customer_guid)
        query.add_guid_match(
            [INVOICE_OWNER, QOF_PARAM_GUID], customer_guid, QOF_QUERY_AND)

    if date_opened_from != None:
        pred_data = gnucash.gnucash_core.QueryDatePredicate(
            QOF_COMPARE_GTE, 2, datetime.datetime.strptime(
                date_opened_from, "%Y-%m-%d").date())
        query.add_term(['date_opened'], pred_data, QOF_QUERY_AND)

    if date_opened_to != None:
        pred_data = gnucash.gnucash_core.QueryDatePredicate(
            QOF_COMPARE_LTE, 2, datetime.datetime.strptime(
                date_opened_to, "%Y-%m-%d").date())
        query.add_term(['date_opened'], pred_data, QOF_QUERY_AND)

    # return only bills (2 = bills)
    pred_data = gnucash.gnucash_core.QueryInt32Predicate(QOF_COMPARE_EQUAL, 2)
    query.add_term([INVOICE_TYPE], pred_data, QOF_QUERY_AND)

    bills = []

    for result in query.run():
        bills.append(gnucash_simple.billToDict(
            gnucash.gnucash_business.Bill(instance=result)))

    query.destroy()

    return bills

def getGnuCashInvoice(book ,id):

    # we don't use book.InvoicelLookupByID(id) as this is identical to
    # book.BillLookupByID(id) so can return the same object if they share IDs

    query = gnucash.Query()
    query.search_for('gncInvoice')
    query.set_book(book)

    # return only invoices (1 = invoices)
    pred_data = gnucash.gnucash_core.QueryInt32Predicate(QOF_COMPARE_EQUAL, 1)
    query.add_term([INVOICE_TYPE], pred_data, QOF_QUERY_AND)

    INVOICE_ID = 'id'

    pred_data = gnucash.gnucash_core.QueryStringPredicate(
        QOF_COMPARE_EQUAL, id, QOF_STRING_MATCH_NORMAL, False)
    query.add_term([INVOICE_ID], pred_data, QOF_QUERY_AND)

    invoice = None

    for result in query.run():
        invoice = gnucash.gnucash_business.Invoice(instance=result)

    query.destroy()

    return invoice

def getGnuCashBill(book ,id):

    # we don't use book.InvoicelLookupByID(id) as this is identical to
    # book.BillLookupByID(id) so can return the same object if they share IDs

    query = gnucash.Query()
    query.search_for('gncInvoice')
    query.set_book(book)

    # return only bills (2 = bills)
    pred_data = gnucash.gnucash_core.QueryInt32Predicate(QOF_COMPARE_EQUAL, 2)
    query.add_term([INVOICE_TYPE], pred_data, QOF_QUERY_AND)

    INVOICE_ID = 'id'

    pred_data = gnucash.gnucash_core.QueryStringPredicate(
        QOF_COMPARE_EQUAL, id, QOF_STRING_MATCH_NORMAL, False)
    query.add_term([INVOICE_ID], pred_data, QOF_QUERY_AND)

    bill = None

    for result in query.run():
        bill = gnucash.gnucash_business.Bill(instance=result)

    query.destroy()

    return bill

def getInvoice(book, id):

    return gnucash_simple.invoiceToDict(getGnuCashInvoice(book, id))

def payInvoice(book, id, posted_account_guid, transfer_account_guid,
    payment_date, memo, num, auto_pay):

    invoice = getGnuCashInvoice(book, id)
    
    account_guid2 = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(transfer_account_guid, account_guid2)

    xfer_acc = account_guid2.AccountLookup(session.book)

    invoice.ApplyPayment(None, xfer_acc, invoice.GetTotal(), GncNumeric(0),
        datetime.datetime.strptime(payment_date, '%Y-%m-%d'), memo, num)

    return gnucash_simple.invoiceToDict(invoice)    

def payBill(book, id, posted_account_guid, transfer_account_guid, payment_date,
    memo, num, auto_pay):

    bill = getGnuCashBill(book, id)

    account_guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(transfer_account_guid, account_guid)

    xfer_acc = account_guid.AccountLookup(session.book)

    # We pay the negative total as the bill as this seemed to cause issues
    # with the split not being set correctly and not being marked as paid
    bill.ApplyPayment(None, xfer_acc, bill.GetTotal().neg(), GncNumeric(0),
        datetime.datetime.strptime(payment_date, '%Y-%m-%d'), memo, num)

    return gnucash_simple.billToDict(bill)

def getBill(book, id):

    return gnucash_simple.billToDict(getGnuCashBill(book, id))

def addVendor(book, id, currency_mnumonic, name, contact, address_line_1,
    address_line_2, address_line_3, address_line_4, phone, fax, email):

    if name == '':
        raise Error('NoVendorName', 'A name must be entered for this company',
            {'field': 'name'})

    if (address_line_1 == ''
        and address_line_2 == ''
        and address_line_3 == ''
        and address_line_4 == ''):
        raise Error('NoVendorAddress',
            'An address must be entered for this company',
            {'field': 'address'})

    commod_table = book.get_table()
    currency = commod_table.lookup('CURRENCY', currency_mnumonic)

    if currency is None:
        raise Error('InvalidVendorCurrency',
            'A valid currency must be supplied for this vendor',
            {'field': 'currency'})

    if id is None:
        id = book.VendorNextID()

    vendor = Vendor(session.book, id, currency, name)

    address = vendor.GetAddr()
    address.SetName(contact)
    address.SetAddr1(address_line_1)
    address.SetAddr2(address_line_2)
    address.SetAddr3(address_line_3)
    address.SetAddr4(address_line_4)
    address.SetPhone(phone)
    address.SetFax(fax)
    address.SetEmail(email)

    return gnucash_simple.vendorToDict(vendor)

def addCustomer(book, id, currency_mnumonic, name, contact, address_line_1,
    address_line_2, address_line_3, address_line_4, phone, fax, email):

    if name == '':
        raise Error('NoCustomerName',
            'A name must be entered for this company', {'field': 'name'})

    if (address_line_1 == ''
        and address_line_2 == ''
        and address_line_3 == ''
        and address_line_4 == ''):
        raise Error('NoCustomerAddress',
            'An address must be entered for this company',
            {'field': 'address'})

    commod_table = book.get_table()
    currency = commod_table.lookup('CURRENCY', currency_mnumonic)

    if currency is None:
        raise Error('InvalidCustomerCurrency',
            'A valid currency must be supplied for this customer',
            {'field': 'currency'})

    if id is None:
        id = book.CustomerNextID()

    customer = Customer(session.book, id, currency, name)

    address = customer.GetAddr()
    address.SetName(contact)
    address.SetAddr1(address_line_1)
    address.SetAddr2(address_line_2)
    address.SetAddr3(address_line_3)
    address.SetAddr4(address_line_4)
    address.SetPhone(phone)
    address.SetFax(fax)
    address.SetEmail(email)

    return gnucash_simple.customerToDict(customer)

def updateCustomer(book, id, name, contact, address_line_1, address_line_2,
    address_line_3, address_line_4, phone, fax, email):

    customer = book.CustomerLookupByID(id)

    if customer is None:
        raise Error('NoCustomer', 'A customer with this ID does not exist',
            {'field': 'id'})

    if name == '':
        raise Error('NoCustomerName',
            'A name must be entered for this company', {'field': 'name'})

    if (address_line_1 == ''
        and address_line_2 == ''
        and address_line_3 == ''
        and address_line_4 == ''):
        raise Error('NoCustomerAddress',
            'An address must be entered for this company',
            {'field': 'address'})

    customer.SetName(name)

    address = customer.GetAddr()
    address.SetName(contact)
    address.SetAddr1(address_line_1)
    address.SetAddr2(address_line_2)
    address.SetAddr3(address_line_3)
    address.SetAddr4(address_line_4)
    address.SetPhone(phone)
    address.SetFax(fax)
    address.SetEmail(email)

    return gnucash_simple.customerToDict(customer)

def addInvoice(book, id, customer_id, currency_mnumonic, date_opened, notes):

    customer = book.CustomerLookupByID(customer_id)

    if customer is None:
        raise Error('NoCustomer',
            'A customer with this ID does not exist', {'field': 'id'})

    if id is None:
        id = book.InvoiceNextID(customer)

    try:
        date_opened = datetime.datetime.strptime(date_opened, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidDateOpened',
            'The date opened must be provided in the form YYYY-MM-DD',
            {'field': 'date_opened'})

    if currency_mnumonic is None:
        currency_mnumonic = customer.GetCurrency().get_mnemonic()

    commod_table = book.get_table()
    currency = commod_table.lookup('CURRENCY', currency_mnumonic)

    if currency is None:
        raise Error('InvalidCustomerCurrency',
            'A valid currency must be supplied for this customer',
            {'field': 'currency'})

    invoice = Invoice(book, id, currency, customer, date_opened.date())

    invoice.SetNotes(notes)

    return gnucash_simple.invoiceToDict(invoice)

def updateInvoice(book, id, customer_id, currency_mnumonic, date_opened,
    notes, posted, posted_account_guid, posted_date, due_date, posted_memo,
    posted_accumulatesplits, posted_autopay):

    invoice = getGnuCashInvoice(book, id)

    if invoice is None:
        raise Error('NoInvoice',
            'An invoice with this ID does not exist',
            {'field': 'id'})

    customer = book.CustomerLookupByID(customer_id)

    if customer is None:
        raise Error('NoCustomer', 'A customer with this ID does not exist',
            {'field': 'customer_id'})

    try:
        date_opened = datetime.datetime.strptime(date_opened, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidDateOpened',
            'The date opened must be provided in the form YYYY-MM-DD',
            {'field': 'date_opened'})

    if posted_date == '':
        if posted == 1:
            raise Error('NoDatePosted',
                'The date posted must be supplied when posted=1',
                {'field': 'date_posted'})
    else:
        try:
            posted_date = datetime.datetime.strptime(posted_date, "%Y-%m-%d")
        except ValueError:
            raise Error('InvalidDatePosted',
                'The date posted must be provided in the form YYYY-MM-DD',
                {'field': 'posted_date'})

    if due_date == '':
        if posted == 1:
            raise Error('NoDatePosted',
                'The due date must be supplied when posted=1',
                {'field': 'date_posted'})
    else:
        try:
            due_date = datetime.datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            raise Error('InvalidDatePosted',
                'The due date must be provided in the form YYYY-MM-DD',
                {'field': 'due_date'})

    if posted_account_guid == '':
        if posted == 1:
            raise Error('NoPostedAccountGuid',
                'The posted account GUID must be supplied when posted=1',
                {'field': 'posted_account_guid'})
    else:
        guid = gnucash.gnucash_core.GUID() 
        gnucash.gnucash_core.GUIDString(posted_account_guid, guid)

        posted_account = guid.AccountLookup(book)

        if posted_account is None:
            raise Error('NoAccount',
                'No account exists with the posted account GUID',
                {'field': 'posted_account_guid'})

    invoice.SetOwner(customer)
    invoice.SetDateOpened(date_opened)
    invoice.SetNotes(notes)

    # post if currently unposted and posted=1
    if (invoice.GetDatePosted().strftime('%Y-%m-%d') == '1970-01-01'
        and posted == 1):
        invoice.PostToAccount(posted_account, posted_date, due_date,
            posted_memo, posted_accumulatesplits, posted_autopay)

    return gnucash_simple.invoiceToDict(invoice)

def updateBill(book, id, vendor_id, currency_mnumonic, date_opened, notes,
    posted, posted_account_guid, posted_date, due_date, posted_memo,
    posted_accumulatesplits, posted_autopay):

    bill = getGnuCashBill(book, id)

    if bill is None:
        raise Error('NoBill', 'A bill with this ID does not exist',
            {'field': 'id'})

    vendor = book.VendorLookupByID(vendor_id)

    if vendor is None:
        raise Error('NoVendor',
            'A vendor with this ID does not exist',
            {'field': 'vendor_id'})

    try:
        date_opened = datetime.datetime.strptime(date_opened, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidDateOpened',
            'The date opened must be provided in the form YYYY-MM-DD',
            {'field': 'date_opened'})

    if posted_date == '':
        if posted == 1:
            raise Error('NoDatePosted',
                'The date posted must be supplied when posted=1',
                {'field': 'date_posted'})
    else:
        try:
            posted_date = datetime.datetime.strptime(posted_date, "%Y-%m-%d")
        except ValueError:
            raise Error('InvalidDatePosted',
                'The date posted must be provided in the form YYYY-MM-DD',
                {'field': 'posted_date'})

    if due_date == '':
        if posted == 1:
            raise Error('NoDatePosted',
                'The due date must be supplied when posted=1',
                {'field': 'date_posted'})
    else:
        try:
            due_date = datetime.datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            raise Error('InvalidDatePosted',
                'The due date must be provided in the form YYYY-MM-DD',
                {'field': 'due_date'})

    if posted_account_guid == '':
        if posted == 1:
            raise Error('NoPostedAccountGuid',
                'The posted account GUID must be supplied when posted=1',
                {'field': 'posted_account_guid'})
    else:
        guid = gnucash.gnucash_core.GUID() 
        gnucash.gnucash_core.GUIDString(posted_account_guid, guid)

        posted_account = guid.AccountLookup(book)

        if posted_account is None:
            raise Error('NoAccount',
                'No account exists with the posted account GUID',
                {'field': 'posted_account_guid'})

    bill.SetOwner(vendor)
    bill.SetDateOpened(date_opened)
    bill.SetNotes(notes)

    # post if currently unposted and posted=1
    if bill.GetDatePosted().strftime('%Y-%m-%d') == '1970-01-01' and posted == 1:
        bill.PostToAccount(posted_account, posted_date, due_date, posted_memo,
            posted_accumulatesplits, posted_autopay)

    return gnucash_simple.billToDict(bill)

def addEntry(book, invoice_id, date, description, account_guid, quantity, price):

    invoice = getGnuCashInvoice(book, invoice_id)

    if invoice is None:
        raise Error('NoInvoice',
            'No invoice exists with this ID', {'field': 'invoice_id'})

    try:
        date = datetime.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidDateOpened',
            'The date opened must be provided in the form YYYY-MM-DD',
            {'field': 'date'})

    guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(account_guid, guid)

    account = guid.AccountLookup(book)

    if account is None:
        raise Error('NoAccount', 'No account exists with this GUID',
            {'field': 'account_guid'})

    try:
        quantity = Decimal(quantity).quantize(Decimal('.01'))
    except ArithmeticError:
        raise Error('InvalidQuantity', 'This quantity is not valid',
            {'field': 'quantity'})

    try:
        price = Decimal(price).quantize(Decimal('.01'))
    except ArithmeticError:
        raise Error('InvalidPrice', 'This price is not valid',
            {'field': 'price'})

    entry = Entry(book, invoice, date.date())
    entry.SetDateEntered(datetime.datetime.now())
    entry.SetDescription(description)
    entry.SetInvAccount(account)
    entry.SetQuantity(gnc_numeric_from_decimal(quantity))
    entry.SetInvPrice(gnc_numeric_from_decimal(price))

    return gnucash_simple.entryToDict(entry)

def addBillEntry(book, bill_id, date, description, account_guid, quantity,
    price):

    bill = getGnuCashBill(book,bill_id)

    if bill is None:
        raise Error('NoBill', 'No bill exists with this ID',
            {'field': 'bill_id'})

    try:
        date = datetime.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidDateOpened',
            'The date opened must be provided in the form YYYY-MM-DD',
            {'field': 'date'})

    guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(account_guid, guid)

    account = guid.AccountLookup(book)

    if account is None:
        raise Error('NoAccount', 'No account exists with this GUID',
            {'field': 'account_guid'})

    try:
        quantity = Decimal(quantity).quantize(Decimal('.01'))
    except ArithmeticError:
        raise Error('InvalidQuantity', 'This quantity is not valid',
            {'field': 'quantity'})

    try:
        price = Decimal(price).quantize(Decimal('.01'))
    except ArithmeticError:
        raise Error('InvalidPrice', 'This price is not valid',
            {'field': 'price'})
    
    entry = Entry(book, bill, date.date())
    entry.SetDateEntered(datetime.datetime.now())
    entry.SetDescription(description)
    entry.SetBillAccount(account)
    entry.SetQuantity(gnc_numeric_from_decimal(quantity))
    entry.SetBillPrice(gnc_numeric_from_decimal(price))

    return gnucash_simple.entryToDict(entry)

def getEntry(book, entry_guid):

    guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(entry_guid, guid)

    entry = book.EntryLookup(guid)

    if entry is None:
        return None
    else:
        return gnucash_simple.entryToDict(entry)

def updateEntry(book, entry_guid, date, description, account_guid, quantity,
    price):

    guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(entry_guid, guid)

    entry = book.EntryLookup(guid)

    if entry is None:
        raise Error('NoEntry', 'No entry exists with this GUID',
            {'field': 'entry_guid'})

    try:
        date = datetime.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidDateOpened',
            'The date opened must be provided in the form YYYY-MM-DD',
            {'field': 'date'})
 
    gnucash.gnucash_core.GUIDString(account_guid, guid)

    account = guid.AccountLookup(book)

    if account is None:
        raise Error('NoAccount', 'No account exists with this GUID',
            {'field': 'account_guid'})

    entry.SetDate(date.date())
    entry.SetDateEntered(datetime.datetime.now())
    entry.SetDescription(description)
    entry.SetInvAccount(account)
    entry.SetQuantity(
        gnc_numeric_from_decimal(Decimal(quantity).quantize(Decimal('.01'))))
    entry.SetInvPrice(
        gnc_numeric_from_decimal(Decimal(price).quantize(Decimal('.01'))))

    return gnucash_simple.entryToDict(entry)

def deleteEntry(book, entry_guid):

    guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(entry_guid, guid)

    entry = book.EntryLookup(guid)

    invoice = entry.GetInvoice()
    bill = entry.GetBill()

    if invoice != None and entry != None:
        invoice.RemoveEntry(entry)
    elif bill != None and entry != None:
        bill.RemoveEntry(entry)

    if entry != None:
        entry.Destroy()

def deleteTransaction(book, transaction_guid):

    guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(transaction_guid, guid)

    transaction = guid.TransLookup(book)

    if transaction != None :
        transaction.Destroy()

def addBill(book, id, vendor_id, currency_mnumonic, date_opened, notes):

    vendor = book.VendorLookupByID(vendor_id)

    if vendor is None:
        raise Error('NoVendor', 'A vendor with this ID does not exist',
            {'field': 'id'})

    if id is None:
        id = book.BillNextID(vendor)

    try:
        date_opened = datetime.datetime.strptime(date_opened, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidVendorDateOpened',
            'The date opened must be provided in the form YYYY-MM-DD',
            {'field': 'date_opened'})

    if currency_mnumonic is None:
        currency_mnumonic = vendor.GetCurrency().get_mnemonic()

    commod_table = book.get_table()
    currency = commod_table.lookup('CURRENCY', currency_mnumonic)

    if currency is None:
        raise Error('InvalidVendorCurrency',
            'A valid currency must be supplied for this vendor',
            {'field': 'currency'})

    bill = Bill(book, id, currency, vendor, date_opened.date())

    bill.SetNotes(notes)

    return gnucash_simple.billToDict(bill)

def addAccount(book, name, currency_mnumonic, account_type_id,
    parent_account_guid, description, code):

    from gnucash.gnucash_core_c import ACCT_TYPE_ROOT, ACCT_TYPE_TRADING

    if name == '':
        raise Error('NoAccountName',
            'A name must be entered for this account',
            {'field': 'name'})

    try:
        account_type_id = int(account_type_id)
    except (TypeError, ValueError):
        raise Error('InvalidAccountTypeID',
            'A valid account type id must be supplied for this account',
            {'field': 'account_type_id'})

    # account types above TRADING (CHECKING, SAVINGS, MONEYMRKT, CREDITLINE)
    # are aliases that the engine refuses to set - see Account.h
    if (account_type_id < 0 or account_type_id > ACCT_TYPE_TRADING
            or account_type_id == ACCT_TYPE_ROOT):
        raise Error('InvalidAccountTypeID',
            'A valid account type id must be supplied for this account',
            {'field': 'account_type_id'})

    commod_table = book.get_table()
    currency = commod_table.lookup('CURRENCY', currency_mnumonic)

    if currency is None:
        raise Error('InvalidAccountCurrency',
            'A valid currency must be supplied for this account',
            {'field': 'currency'})

    if parent_account_guid == '':
        raise Error('NoParentAccount',
            'A parent account guid must be supplied for this account',
            {'field': 'parent_account_guid'})

    guid = gnucash.gnucash_core.GUID()
    gnucash.gnucash_core.GUIDString(parent_account_guid, guid)
    parent_account = guid.AccountLookup(book)

    if parent_account is None:
        raise Error('InvalidParentAccount',
            'A parent account with this guid does not exist',
            {'field': 'parent_account_guid'})

    account = Account(book)
    parent_account.append_child(account)
    account.SetName(name)
    account.SetType(account_type_id)
    account.SetCommodity(currency)

    if description != '':
        account.SetDescription(description)

    if code != '':
        account.SetCode(code)

    return gnucash_simple.accountToDict(account)

def addTransaction(book, num, description, date_posted, currency_mnumonic, splits):

    transaction = Transaction(book)

    transaction.BeginEdit()

    commod_table = book.get_table()
    currency = commod_table.lookup('CURRENCY', currency_mnumonic)

    if currency is None:
        raise Error('InvalidTransactionCurrency',
            'A valid currency must be supplied for this transaction',
            {'field': 'currency'})

    try:
        date_posted = datetime.datetime.strptime(date_posted, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidDatePosted',
            'The date posted must be provided in the form YYYY-MM-DD',
            {'field': 'date_posted'})


    for split_values in splits:
        account_guid = gnucash.gnucash_core.GUID() 
        gnucash.gnucash_core.GUIDString(split_values['account_guid'], account_guid)

        account = account_guid.AccountLookup(book)

        if account is None:
            raise Error('InvalidSplitAccount',
                'A valid account must be supplied for this split',
                {'field': 'account'})

        split = Split(book)
        split.SetValue(GncNumeric(split_values['value'], 100))
        split.SetAccount(account)
        split.SetParent(transaction)

    transaction.SetCurrency(currency)
    transaction.SetDescription(description)
    transaction.SetNum(num)

    transaction.SetDatePostedTS(date_posted)

    transaction.CommitEdit()

    return gnucash_simple.transactionToDict(transaction, ['splits'])

def getTransaction(book, transaction_guid):

    guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(transaction_guid, guid)

    transaction = guid.TransLookup(book)

    if transaction is None:
        return None
    else:
        return gnucash_simple.transactionToDict(transaction, ['splits'])

def editTransaction(book, transaction_guid, num, description, date_posted,
    currency_mnumonic, splits):

    guid = gnucash.gnucash_core.GUID() 
    gnucash.gnucash_core.GUIDString(transaction_guid, guid)

    transaction = guid.TransLookup(book)

    if transaction is None:
        raise Error('NoCustomer',
            'A transaction with this GUID does not exist',
            {'field': 'guid'})

    transaction.BeginEdit()

    commod_table = book.get_table()
    currency = commod_table.lookup('CURRENCY', currency_mnumonic)

    if currency is None:
        raise Error('InvalidTransactionCurrency',
            'A valid currency must be supplied for this transaction',
            {'field': 'currency'})


    try:
        date_posted = datetime.datetime.strptime(date_posted, "%Y-%m-%d")
    except ValueError:
        raise Error('InvalidDatePosted',
            'The date posted must be provided in the form YYYY-MM-DD',
            {'field': 'date_posted'})

    for split_values in splits:

        split_guid = gnucash.gnucash_core.GUID() 
        gnucash.gnucash_core.GUIDString(split_values['guid'], split_guid)

        split = split_guid.SplitLookup(book)

        if split is None:
            raise Error('InvalidSplitGuid',
                'A valid guid must be supplied for this split',
                {'field': 'guid'})

        account_guid = gnucash.gnucash_core.GUID() 
        gnucash.gnucash_core.GUIDString(
            split_values['account_guid'], account_guid)

        account = account_guid.AccountLookup(book)

        if account is None:
            raise Error('InvalidSplitAccount',
                'A valid account must be supplied for this split',
                {'field': 'account'})

        split.SetValue(GncNumeric(split_values['value'], 100))
        split.SetAccount(account)
        split.SetParent(transaction)

    transaction.SetCurrency(currency)
    transaction.SetDescription(description)
    transaction.SetNum(num)

    transaction.SetDatePostedTS(date_posted)

    transaction.CommitEdit()

    return gnucash_simple.transactionToDict(transaction, ['splits'])

def lookupCommodity(book, namespace, mnemonic, field='commodity',
    error_type='InvalidCommodity'):

    if not namespace:
        raise Error(error_type,
            'A commodity namespace must be supplied',
            {'field': field + '_namespace'})
    if not mnemonic:
        raise Error(error_type,
            'A commodity mnemonic must be supplied',
            {'field': field + '_mnemonic'})

    commodity = book.get_table().lookup(namespace, mnemonic)

    if commodity is None:
        raise Error(error_type,
            'No commodity exists with namespace ' + namespace +
            ' and mnemonic ' + mnemonic,
            {'field': field})

    return commodity

def getCommodities(book, namespace=None):

    commod_table = book.get_table()

    if namespace:
        namespaces = [namespace]
    else:
        namespaces = commod_table.get_namespaces()

    result = []
    for ns in namespaces:
        for commodity in commod_table.get_commodities(ns):
            result.append(gnucash_simple.commodityToDict(commodity))

    return result

def getCommodity(book, namespace, mnemonic):

    commodity = book.get_table().lookup(namespace, mnemonic)

    if commodity is None:
        return None

    return gnucash_simple.commodityToDict(commodity)

def addCommodity(book, namespace, mnemonic, fullname, cusip, fraction,
    quote_source, quote_tz):

    if not namespace:
        raise Error('NoCommodityNamespace',
            'A namespace must be supplied for this commodity',
            {'field': 'namespace'})
    if not mnemonic:
        raise Error('NoCommodityMnemonic',
            'A mnemonic must be supplied for this commodity',
            {'field': 'mnemonic'})
    if not fullname:
        raise Error('NoCommodityFullname',
            'A fullname must be supplied for this commodity',
            {'field': 'fullname'})

    try:
        fraction = int(fraction)
    except (TypeError, ValueError):
        raise Error('InvalidCommodityFraction',
            'The fraction must be a positive integer',
            {'field': 'fraction'})
    if fraction <= 0:
        raise Error('InvalidCommodityFraction',
            'The fraction must be a positive integer',
            {'field': 'fraction'})

    commod_table = book.get_table()

    if commod_table.lookup(namespace, mnemonic) is not None:
        raise Error('CommodityExists',
            'A commodity with this namespace and mnemonic already exists',
            {'field': 'mnemonic'})

    commodity = GncCommodity(book, fullname, namespace, mnemonic, cusip,
        fraction)
    inserted = commod_table.insert(commodity)

    if inserted is None:
        raise Error('CommodityInsertFailed',
            'The commodity could not be inserted into the commodity table',
            {'field': 'mnemonic'})

    if quote_source:
        source = gnucash.gnucash_core_c.gnc_quote_source_lookup_by_internal(
            quote_source)
        if source is not None:
            inserted.set_quote_flag(True)
            inserted.set_quote_source(source)
    if quote_tz:
        inserted.set_quote_tz(quote_tz)

    return gnucash_simple.commodityToDict(inserted)

def _parsePriceDate(date_str, field='date'):
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        pass
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise Error('InvalidPriceDate',
            'The date must be provided as YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS',
            {'field': field})

def _priceMatchesFilter(price, currency_mnemonic, date_from, date_to):
    if currency_mnemonic:
        if price.get_currency().get_mnemonic() != currency_mnemonic:
            return False
    if date_from or date_to:
        price_time = price.get_time64()
        if not isinstance(price_time, datetime.datetime):
            price_time = datetime.datetime.fromtimestamp(int(price_time))
        # Compare on date only so a YYYY-MM-DD range is inclusive of whole days.
        price_date = price_time.date()
        if date_from and price_date < _parsePriceDate(
                date_from, 'date_from').date():
            return False
        if date_to and price_date > _parsePriceDate(
                date_to, 'date_to').date():
            return False
    return True

def getPrices(book, commodity_namespace, commodity_mnemonic,
    currency_mnemonic, date_from, date_to):

    pricedb = book.get_price_db()
    commod_table = book.get_table()
    result = []

    if commodity_namespace and commodity_mnemonic:
        commodity = lookupCommodity(book, commodity_namespace,
            commodity_mnemonic, field='commodity',
            error_type='InvalidPriceCommodity')
        commodities = [commodity]
    else:
        commodities = []
        for ns in commod_table.get_namespaces():
            commodities.extend(commod_table.get_commodities(ns))

    for commodity in commodities:
        count = pricedb.num_prices(commodity)
        for i in range(count):
            price = pricedb.nth_price(commodity, i)
            if price is None:
                continue
            if _priceMatchesFilter(price, currency_mnemonic, date_from,
                date_to):
                result.append(gnucash_simple.priceToDict(price))

    return result

# Linear scan - the pricedb has no lookup-by-GUID. Adequate for example use;
# would need a SWIG-side index for large books.
def getPrice(book, guid):

    pricedb = book.get_price_db()
    commod_table = book.get_table()

    for ns in commod_table.get_namespaces():
        for commodity in commod_table.get_commodities(ns):
            count = pricedb.num_prices(commodity)
            for i in range(count):
                price = pricedb.nth_price(commodity, i)
                if price is None:
                    continue
                if price.GetGUID().to_string() == guid:
                    return gnucash_simple.priceToDict(price)

    return None

def _findPriceByGUID(book, guid):

    pricedb = book.get_price_db()
    commod_table = book.get_table()

    for ns in commod_table.get_namespaces():
        for commodity in commod_table.get_commodities(ns):
            count = pricedb.num_prices(commodity)
            for i in range(count):
                price = pricedb.nth_price(commodity, i)
                if price is None:
                    continue
                if price.GetGUID().to_string() == guid:
                    return price

    return None

def addPrice(book, commodity_namespace, commodity_mnemonic, currency_mnemonic,
    value, value_num, value_denom, date, source, price_type):

    commodity = lookupCommodity(book, commodity_namespace, commodity_mnemonic,
        field='commodity', error_type='InvalidPriceCommodity')
    currency = lookupCommodity(book, 'CURRENCY', currency_mnemonic,
        field='currency', error_type='InvalidPriceCurrency')

    if value_num is not None and value_denom is not None:
        try:
            num = int(value_num)
            denom = int(value_denom)
        except (TypeError, ValueError):
            raise Error('InvalidPriceValue',
                'value_num and value_denom must be integers',
                {'field': 'value_num'})
        if denom == 0:
            raise Error('InvalidPriceValue',
                'value_denom must be non-zero',
                {'field': 'value_denom'})
        gnc_value = GncNumeric(num, denom)
    else:
        if not value:
            raise Error('NoPriceValue',
                'A value must be supplied for this price',
                {'field': 'value'})
        try:
            decimal_value = Decimal(value)
        except Exception:
            raise Error('InvalidPriceValue',
                'value must be a valid decimal number',
                {'field': 'value'})
        gnc_value = gnc_numeric_from_decimal(decimal_value)

    if not date:
        raise Error('NoPriceDate',
            'A date must be supplied for this price',
            {'field': 'date'})
    price_datetime = _parsePriceDate(date)

    # The engine only persists prices whose source matches one of the canonical
    # strings in gnc-pricedb.cpp's source_names[]. Anything else is silently
    # dropped, so validate up front.
    valid_sources = {
        'user:price-editor', 'Finance::Quote', 'user:price',
        'user:xfer-dialog', 'user:split-register', 'user:split-import',
        'user:stock-split', 'user:stock-transaction', 'user:invoice-post',
    }
    if source == '':
        source = 'user:price'
    elif source not in valid_sources:
        raise Error('InvalidPriceSource',
            'source must be one of: ' + ', '.join(sorted(valid_sources)),
            {'field': 'source'})

    if price_type == '':
        price_type = 'last'

    price = GncPrice(book)
    price.begin_edit()
    price.set_commodity(commodity)
    price.set_currency(currency)
    price.set_time64(price_datetime)
    price.set_value(gnc_value)
    price.set_source_string(source)
    price.set_typestr(price_type)
    price.commit_edit()

    pricedb = book.get_price_db()
    pricedb.add_price(price)

    return gnucash_simple.priceToDict(price)

def deletePrice(book, guid):

    price = _findPriceByGUID(book, guid)
    if price is None:
        return False

    pricedb = book.get_price_db()
    pricedb.remove_price(price)
    return True

def getLatestPrice(book, namespace, mnemonic, currency_mnemonic):

    commodity = lookupCommodity(book, namespace, mnemonic,
        field='commodity', error_type='InvalidPriceCommodity')
    currency = lookupCommodity(book, 'CURRENCY', currency_mnemonic,
        field='currency', error_type='InvalidPriceCurrency')

    pricedb = book.get_price_db()
    price = pricedb.lookup_latest(commodity, currency)

    if price is None:
        return None

    return gnucash_simple.priceToDict(price)

def getNearestPrice(book, namespace, mnemonic, currency_mnemonic, date):

    commodity = lookupCommodity(book, namespace, mnemonic,
        field='commodity', error_type='InvalidPriceCommodity')
    currency = lookupCommodity(book, 'CURRENCY', currency_mnemonic,
        field='currency', error_type='InvalidPriceCurrency')

    if not date:
        raise Error('NoPriceDate',
            'A date must be supplied for the nearest-price lookup',
            {'field': 'date'})

    price_datetime = _parsePriceDate(date)

    pricedb = book.get_price_db()
    price = pricedb.lookup_nearest_in_time64(commodity, currency,
        price_datetime)

    if price is None:
        return None

    return gnucash_simple.priceToDict(price)

def gnc_numeric_from_decimal(decimal_value):
    sign, digits, exponent = decimal_value.as_tuple()

    # convert decimal digits to a fractional numerator
    # equivalent to
    # numerator = int(''.join(digits))
    # but without the wated conversion to string and back,
    # this is probably the same algorithm int() uses
    numerator = 0
    TEN = int(Decimal(0).radix()) # this is always 10
    numerator_place_value = 1
    # add each digit to the final value multiplied by the place value
    # from least significant to most significant
    for i in range(len(digits)-1,-1,-1):
        numerator += digits[i] * numerator_place_value
        numerator_place_value *= TEN

    if decimal_value.is_signed():
        numerator = -numerator

    # if the exponent is negative, we use it to set the denominator
    if exponent < 0 :
        denominator = TEN ** (-exponent)
    # if the exponent isn't negative, we bump up the numerator
    # and set the denominator to 1
    else:
        numerator *= TEN ** exponent
        denominator = 1

    return GncNumeric(numerator, denominator)

def shutdown():
    # session may be None if a POST /revert failed to re-open the book; guard
    # so the atexit handler can't crash on exit in that degraded state.
    if session is not None:
        session.save()
        session.end()
        session.destroy()
    print('Shutdown')

class Error(Exception):
    """Base class for exceptions in this module."""
    def __init__(self, type, message, data):
        self.type = type
        self.message = message
        self.data = data

try:
    options, arguments = getopt.getopt(sys.argv[1:], 'nh:', ['host=', 'new='])
except getopt.GetoptError as err:
    print(str(err)) # will print something like "option -a not recognized"
    print('Usage: python-rest.py <connection string>')
    sys.exit(2)

if len(arguments) == 0:
    print('Usage: python-rest.py <connection string>')
    sys.exit(2)

#set default host for Flask
host = '127.0.0.1'

#allow host option to be changed
for option, value in options:
    if option in ("-h", "--host"):
        host = value

is_new = False

# allow a new database to be used
for option, value in options:
    if option in ("-n", "--new"):
        is_new = True


# connection string from the command line, reused by /revert to re-open
connection_string = arguments[0]

#start gnucash session base on connection string argument
if is_new:
    session = gnucash.Session(connection_string, SessionOpenMode.SESSION_NEW_STORE)

    # seem to get errors if we use the session directly, so save it and
    #destroy it so it's no longer new

    session.save()
    session.end()
    session.destroy()

# unsure about SESSION_BREAK_LOCK - it used to be ignore_lock=True
session = gnucash.Session(connection_string, SessionOpenMode.SESSION_BREAK_LOCK)

# register method to close gnucash connection gracefully
atexit.register(shutdown)

app.debug = False

# log to console
if not app.debug:
    import logging
    from logging import StreamHandler
    stream_handler = StreamHandler()
    stream_handler.setLevel(logging.ERROR)
    app.logger.addHandler(stream_handler)

# start Flask server
#
# threaded=False is required, not optional: the whole app shares a single
# global GnuCash session/book and the qof engine is not thread-safe. Flask's
# app.run() sets threaded=True by default, which would let concurrent requests
# mutate the shared book underneath each other -- and a POST /revert could
# end()/destroy() the session while another in-flight request is using
# session.book. Serving one request at a time serialises /revert with every
# other handler and removes that whole class of races. (Note: this only holds
# within a single process; do not run this example under a multi-worker WSGI
# server such as gunicorn, where the workers would fight over the backend lock.)
app.run(host=host, threaded=False)

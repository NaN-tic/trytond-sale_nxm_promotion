import unittest
from decimal import Decimal

from proteus import Model
from trytond.modules.account.tests.tools import (
    create_chart, create_fiscalyear, get_accounts)
from trytond.modules.account_invoice.tests.tools import (
    create_payment_term, set_fiscalyear_invoice_sequences)
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestNxmPromotion(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules([
            'sale_promotion', 'sale_nxm_promotion', 'sale_product_package'])

        _ = create_company()
        company = get_company()

        fiscalyear = set_fiscalyear_invoice_sequences(
            create_fiscalyear(company))
        fiscalyear.click('create_period')

        _ = create_chart(company)
        accounts = get_accounts(company)
        revenue = accounts['revenue']
        expense = accounts['expense']

        Party = Model.get('party.party')
        customer = Party(name='Customer')
        customer.save()

        ProductCategory = Model.get('product.category')
        account_category = ProductCategory(name='Account Category')
        account_category.accounting = True
        account_category.account_expense = expense
        account_category.account_revenue = revenue
        account_category.save()

        ProductUom = Model.get('product.uom')
        unit, = ProductUom.find([('name', '=', 'Unit')])

        ProductTemplate = Model.get('product.template')
        Product = Model.get('product.product')
        Package = Model.get('product.package')
        template = ProductTemplate()
        template.name = 'product'
        template.default_uom = unit
        template.sale_uom = unit
        template.type = 'goods'
        template.salable = True
        template.list_price = Decimal('10')
        template.cost_price_method = 'fixed'
        template.account_category = account_category
        template.save()
        product = Product()
        product.template = template
        product.cost_price = Decimal('5')
        product.save()

        template_with_package = ProductTemplate()
        template_with_package.name = 'packaged product'
        template_with_package.default_uom = unit
        template_with_package.sale_uom = unit
        template_with_package.type = 'goods'
        template_with_package.salable = True
        template_with_package.list_price = Decimal('10')
        template_with_package.cost_price_method = 'fixed'
        template_with_package.account_category = account_category
        template_with_package.save()
        packaged_product = Product()
        packaged_product.template = template_with_package
        packaged_product.cost_price = Decimal('5')
        packaged_product.save()

        packaged_product_box = Package()
        packaged_product_box.product = packaged_product
        packaged_product_box.quantity = 25
        packaged_product_box.name = 'packaged product box'
        packaged_product_box.is_default = True
        packaged_product_box.save()

        payment_term = create_payment_term()
        payment_term.save()

        Promotion = Model.get('sale.promotion')
        promotion = Promotion()
        promotion.name = '5+1'
        promotion.company = company
        promotion.unit = unit
        promotion.nxm = True
        promotion.formula = 'unit_price'
        nxm_line = promotion.nxm_lines.new()
        nxm_line.product = product
        nxm_line.quantity = 5
        nxm_line.free_quantity = 1
        promotion.save()

        Sale = Model.get('sale.sale')
        sale = Sale()
        sale.party = customer
        sale.payment_term = payment_term
        sale.invoice_method = 'order'
        sale_line = sale.lines.new()
        sale_line.product = product
        sale_line.quantity = 5
        sale.save()
        sale.reload()

        self.assertEqual(len(sale.lines), 2)
        paid_line, free_line = sale.lines
        self.assertEqual(paid_line.quantity, 5)
        self.assertEqual(paid_line.nxm_requested_quantity, 5)
        self.assertEqual(paid_line.promotion, promotion)
        self.assertEqual(free_line.quantity, 1)
        self.assertTrue(free_line.nxm_generated)
        self.assertEqual(free_line.promotion, promotion)
        self.assertEqual(free_line.discount_rate, Decimal('1.0000'))
        self.assertEqual(free_line.amount, Decimal('0.00'))

        paid_line.quantity = 4
        sale.save()
        sale.reload()

        self.assertEqual(len(sale.lines), 1)
        remaining_line, = sale.lines
        self.assertEqual(remaining_line.quantity, 4)
        self.assertEqual(remaining_line.nxm_requested_quantity, None)
        self.assertEqual(remaining_line.promotion, None)

        remaining_line.quantity = 6
        sale.save()
        sale.reload()

        self.assertEqual(len(sale.lines), 3)
        paid_line, extra_paid_line, free_line = sale.lines
        self.assertEqual(paid_line.quantity, 5)
        self.assertEqual(paid_line.nxm_requested_quantity, 6)
        self.assertEqual(paid_line.promotion, promotion)
        self.assertEqual(extra_paid_line.quantity, 1)
        self.assertTrue(extra_paid_line.nxm_generated)
        self.assertEqual(extra_paid_line.promotion, promotion)
        self.assertEqual(free_line.quantity, 1)
        self.assertTrue(free_line.nxm_generated)
        self.assertEqual(free_line.discount_rate, Decimal('1.0000'))
        self.assertEqual(free_line.amount, Decimal('0.00'))

        sale.click('quote')
        sale.reload()
        self.assertEqual(len(sale.lines), 3)
        paid_line, extra_paid_line, free_line = sale.lines
        self.assertEqual(paid_line.promotion, promotion)
        self.assertEqual(extra_paid_line.promotion, promotion)
        self.assertEqual(free_line.promotion, promotion)

        package_promotion = Promotion()
        package_promotion.name = '5 boxes + 1 box'
        package_promotion.company = company
        package_promotion.unit = unit
        package_promotion.nxm = True
        package_promotion.formula = 'unit_price'
        nxm_line = package_promotion.nxm_lines.new()
        nxm_line.product = packaged_product
        self.assertEqual(nxm_line.default_package, packaged_product_box)
        nxm_line.package_quantity = 5
        nxm_line.free_package_quantity = 1
        self.assertEqual(nxm_line.quantity, 125)
        self.assertEqual(nxm_line.free_quantity, 25)
        package_promotion.save()

        package_sale = Sale()
        package_sale.party = customer
        package_sale.payment_term = payment_term
        package_sale.invoice_method = 'order'
        package_sale_line = package_sale.lines.new()
        package_sale_line.product = packaged_product
        self.assertEqual(package_sale_line.product_package, packaged_product_box)
        package_sale_line.quantity = 150
        package_sale.save()
        package_sale.reload()

        self.assertEqual(len(package_sale.lines), 3)
        paid_line, extra_paid_line, free_line = package_sale.lines
        self.assertEqual(paid_line.quantity, 125)
        self.assertEqual(paid_line.package_quantity, 5)
        self.assertEqual(paid_line.nxm_requested_quantity, 150)
        self.assertEqual(paid_line.promotion, package_promotion)
        self.assertEqual(extra_paid_line.quantity, 25)
        self.assertEqual(extra_paid_line.package_quantity, 1)
        self.assertTrue(extra_paid_line.nxm_generated)
        self.assertEqual(extra_paid_line.promotion, package_promotion)
        self.assertEqual(free_line.quantity, 25)
        self.assertEqual(free_line.package_quantity, 1)
        self.assertTrue(free_line.nxm_generated)
        self.assertEqual(free_line.promotion, package_promotion)
        self.assertEqual(free_line.discount_rate, Decimal('1.0000'))
        self.assertEqual(free_line.amount, Decimal('0.00'))

# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from decimal import Decimal
from math import floor

from trytond.model import ModelSQL, ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.transaction import Transaction


class Sale(metaclass=PoolMeta):
    __name__ = 'sale.sale'

    @fields.depends(
        'lines', 'currency', 'company', 'sale_date', 'price_list',
        methods=['get_tax_amount'])
    def on_change_lines(self):
        super().on_change_lines()

    def _has_nxm_lines_to_cleanup(self):
        for line in self.lines or []:
            if (line.nxm_generated
                    or line.nxm_requested_quantity is not None
                    or line.promotion):
                return True
        return False

    @classmethod
    def _sync_sales_nxm_lines(cls, sales):
        transaction = Transaction()
        if getattr(transaction, '_skip_nxm_sync', False):
            return
        Promotion = Pool().get('sale.promotion')
        has_active_nxm_by_price_list = {}
        to_sync = []
        for sale in sales:
            if sale.state != 'draft':
                continue
            price_list_id = sale.price_list.id if sale.price_list else None
            if price_list_id not in has_active_nxm_by_price_list:
                has_active_nxm_by_price_list[price_list_id] = (
                    Promotion.has_active_nxm_price_list(sale))
            if not has_active_nxm_by_price_list[price_list_id]:
                if sale._has_nxm_lines_to_cleanup():
                    to_sync.append(sale)
                continue
            if sale._has_nxm_lines_to_cleanup():
                to_sync.append(sale)
                continue
            to_sync.append(sale)
        if not to_sync:
            return
        transaction._skip_nxm_sync = True
        try:
            for sale in to_sync:
                sale._sync_nxm_lines()
            cls.save(to_sync)
        finally:
            transaction._skip_nxm_sync = False

    @classmethod
    def create(cls, vlist):
        sales = super().create(vlist)
        if getattr(Transaction(), '_skip_nxm_sync', False):
            return sales
        cls._sync_sales_nxm_lines(sales)
        return sales

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        sale_ids = []
        to_write = []
        for sales, values in zip(actions, actions):
            to_write.extend((sales, values))
            sale_ids.extend(s.id for s in sales)
        super().write(*to_write)
        if getattr(Transaction(), '_skip_nxm_sync', False):
            return
        if sale_ids:
            cls._sync_sales_nxm_lines(cls.browse(list(set(sale_ids))))

    @classmethod
    def quote(cls, sales):
        return super().quote(sales)

    def apply_promotion(self):
        Promotion = Pool().get('sale.promotion')

        for line in self.lines:
            if line.type != 'line':
                continue
            if line.original_unit_price is None:
                line.original_unit_price = line.unit_price

        promotions = Promotion.get_promotions(self)
        for promotion in promotions:
            if not promotion.nxm:
                promotion.apply(self)

    def unapply_promotion(self):
        self._collapse_nxm_lines()
        changed = False
        for line in self.lines:
            if line.type != 'line':
                continue
            if line.original_unit_price is not None:
                line.unit_price = line.original_unit_price
                line.original_unit_price = None
                changed = True
            if line.promotion:
                line.promotion = None
                changed = True
        if changed:
            self.lines = self.lines

    def _sync_nxm_lines(self):
        if not self.lines:
            return

        promotions = Pool().get('sale.promotion').get_nxm_promotions(self)
        normalized_lines = []
        for line in self.lines:
            if line.nxm_generated:
                continue
            normalized_lines.extend(self._get_nxm_lines(line, promotions))
        self.lines = normalized_lines

    def _get_nxm_lines(self, line, promotions):
        if line.type != 'line' or not line.product or not line.unit:
            line.nxm_requested_quantity = None
            line.nxm_parent_line = None
            return [line]

        requested = line.nxm_requested_quantity or line.quantity
        had_nxm_quantity = line.nxm_requested_quantity is not None
        promotion = line.get_nxm_promotion(
            requested_quantity=requested, promotions=promotions)
        if not promotion or not requested or requested <= 0:
            if requested and requested > 0:
                self._set_line_quantity(line, requested)
            line.nxm_requested_quantity = None
            line.nxm_parent_line = None
            if had_nxm_quantity:
                line.promotion = None
            line.original_unit_price = None
            return [line]

        quantities = promotion.get_nxm_line_quantities(
            line, requested_quantity=requested)
        if not quantities:
            self._set_line_quantity(line, requested)
            line.nxm_requested_quantity = None
            line.nxm_parent_line = None
            if had_nxm_quantity:
                line.promotion = None
            line.original_unit_price = None
            return [line]

        line.nxm_requested_quantity = requested
        line.nxm_parent_line = None
        lines = []
        self._set_line_quantity(line, quantities['base_paid'])
        self._mark_promotion_line(line, promotion)
        lines.append(line)

        for quantity in quantities['extra_paid']:
            lines.append(self._new_generated_line(
                    line, quantity, promotion, free=False))
        free_line = self._new_generated_line(
            line, quantities['free'], promotion, free=True)
        if free_line:
            lines.append(free_line)
        return lines

    def _new_generated_line(self, source, quantity, promotion, free):
        if quantity <= 0:
            return None
        line = source.__class__()
        line.nxm_generated = True
        line.nxm_parent_line = None
        line.nxm_requested_quantity = None
        line.sale = self
        line.type = source.type
        line.product = source.product
        line.unit = source.unit
        line.taxes = list(source.taxes or [])
        line.on_change_product()
        line.description = source.description
        self._set_line_quantity(line, quantity, use_on_change=True)
        self._mark_promotion_line(line, promotion)
        if free:
            self._set_line_as_free(line)
        return line

    def _set_line_as_free(self, line):
        original_unit_price = line.unit_price
        line.base_price = line.compute_base_price()
        line.discount_rate = Decimal('1.0000')
        line.on_change_discount_rate()
        if original_unit_price is not None:
            line.original_unit_price = original_unit_price
        line.amount = line.on_change_with_amount()

    def _mark_promotion_line(self, line, promotion):
        line.promotion = promotion
        if line.unit_price is not None:
            line.original_unit_price = line.unit_price

    def _set_line_quantity(self, line, quantity, use_on_change=False):
        discount_rate = line.discount_rate
        discount_amount = line.discount_amount
        line.quantity = quantity
        if use_on_change:
            line.on_change_quantity()
            return
        if ('product_package' in line._fields and 'package_quantity' in line._fields
                and line.product_package and line.product_package.quantity):
                line.package_quantity = (
                    line.quantity / line.product_package.quantity)
        if line.product:
            line.unit_price = line.compute_unit_price()
        line.base_price = line.compute_base_price()
        if discount_rate is not None:
            line.discount_rate = discount_rate
            line.on_change_discount_rate()
        elif discount_amount is not None:
            line.discount_amount = discount_amount
            line.on_change_discount_amount()
        line.amount = line.on_change_with_amount()

    def _collapse_nxm_lines(self):
        if not self.lines:
            return
        lines = []
        for line in self.lines:
            if line.nxm_generated:
                continue
            if line.nxm_requested_quantity:
                self._set_line_quantity(line, line.nxm_requested_quantity)
            line.nxm_requested_quantity = None
            line.nxm_parent_line = None
            line.original_unit_price = None
            lines.append(line)
        self.lines = lines


class Promotion(metaclass=PoolMeta):
    __name__ = 'sale.promotion'

    nxm = fields.Boolean(
        "N+M Promotion",
        help="Generate free sale lines instead of changing unit prices.")
    nxm_lines = fields.One2Many(
        'sale.promotion.nxm.line', 'promotion', "N+M Lines",
        states={
            'invisible': ~Eval('nxm', False),
            })

    @classmethod
    def __setup__(cls):
        super().__setup__()
        readonly = Eval('nxm', False)
        if cls.formula.states.get('readonly'):
            cls.formula.states['readonly'] |= readonly
        else:
            cls.formula.states['readonly'] = readonly

    @fields.depends('nxm')
    def on_change_nxm(self):
        if self.nxm:
            self.formula = 'unit_price'

    def apply(self, sale):
        if self.nxm:
            return
        super().apply(sale)

    def _get_nxm_line(self, line):
        for nxm_line in self.nxm_lines:
            if nxm_line.product == line.product:
                return nxm_line

    def _get_nxm_quantities(self, line):
        nxm_line = self._get_nxm_line(line)
        if not nxm_line:
            return None, None
        buy_quantity = nxm_line.quantity or 0
        free_quantity = nxm_line.free_quantity or 0
        buy_package_quantity = nxm_line.package_quantity or 0
        free_package_quantity = nxm_line.free_package_quantity or 0

        if buy_quantity and free_quantity:
            return buy_quantity, free_quantity
        if (not buy_quantity and not buy_package_quantity):
            return None, None
        if (not free_quantity and not free_package_quantity):
            return None, None

        package = nxm_line.default_package
        if not package:
            if 'product_package' in line._fields and line.product_package:
                package = line.product_package
            elif 'default_package' in line.product._fields:
                package = line.product.default_package
        if not package or not package.quantity:
            return None, None

        Uom = Pool().get('product.uom')
        target_unit = self.unit or line.unit
        package_quantity = Uom.compute_qty(
            line.product.default_uom, package.quantity, target_unit,
            round=False)
        return (
            buy_quantity or (buy_package_quantity * package_quantity),
            free_quantity or (free_package_quantity * package_quantity),
        )

    def _get_nxm_sort_quantity(self):
        quantities = [
            (line.quantity or 0) + (line.package_quantity or 0)
            for line in self.nxm_lines]
        return max(quantities or [0])

    @classmethod
    def get_nxm_promotions(cls, sale):
        return sorted(
            (p for p in cls.search(cls._promotions_domain(sale)) if p.nxm),
            key=lambda p: (
                bool(p.nxm_lines), bool(p.products), bool(p.categories),
                bool(p.price_list),
                p._get_nxm_sort_quantity()),
            reverse=True)

    @classmethod
    def has_active_nxm_price_list(cls, sale):
        Date = Pool().get('ir.date')
        if not sale.price_list:
            return False
        with Transaction().set_context(company=sale.company.id):
            sale_date = sale.sale_date or Date.today()
        return bool(cls.search([
                    ('price_list', '=', sale.price_list.id),
                    ('company', '=', sale.company.id),
                    ('nxm', '=', True),
                    ['OR',
                        ('start_date', '<=', sale_date),
                        ('start_date', '=', None),
                        ],
                    ['OR',
                        ('end_date', '=', None),
                        ('end_date', '>=', sale_date),
                        ],
                    ], limit=1))

    @classmethod
    def get_nxm_promotion(
            cls, sale, line, requested_quantity=None, promotions=None):
        if promotions is None:
            promotions = cls.get_nxm_promotions(sale)
        for promotion in promotions:
            if promotion.is_valid_nxm_line(
                    line, requested_quantity=requested_quantity):
                return promotion

    def is_valid_nxm_line(self, line, requested_quantity=None):
        buy_quantity, free_quantity = self._get_nxm_quantities(line)
        if not self.nxm or not buy_quantity or not free_quantity:
            return False
        if (not line.product or not line.unit
                or line.quantity is None or line.unit_price is None):
            return False
        if self.unit and line.unit.category != self.unit.category:
            return False
        if not self.is_valid_sale_line(line):
            return False
        requested_quantity = requested_quantity or line.quantity or 0
        return self._requested_quantity_in_unit(
            line, requested_quantity) >= buy_quantity

    def _requested_quantity_in_unit(self, line, requested_quantity):
        Uom = Pool().get('product.uom')
        if self.unit:
            return Uom.compute_qty(
                line.unit, requested_quantity, self.unit, round=False)
        return requested_quantity

    def _quantity_from_promotion_unit(self, line, quantity):
        Uom = Pool().get('product.uom')
        if self.unit:
            return Uom.compute_qty(self.unit, quantity, line.unit, round=False)
        return quantity

    def get_nxm_line_quantities(self, line, requested_quantity=None):
        buy_quantity, free_quantity = self._get_nxm_quantities(line)
        requested_quantity = requested_quantity or line.quantity or 0
        promotion_quantity = self._requested_quantity_in_unit(
            line, requested_quantity)
        if not buy_quantity or not free_quantity:
            return
        if promotion_quantity < buy_quantity:
            return

        block_count = floor(promotion_quantity / buy_quantity)
        if block_count <= 0:
            return

        remainder = promotion_quantity - (block_count * buy_quantity)
        base_paid = self._quantity_from_promotion_unit(
            line, block_count * buy_quantity)
        extra_paid = []
        if remainder > 0:
            extra_paid.append(self._quantity_from_promotion_unit(
                    line, remainder))
        free = self._quantity_from_promotion_unit(
            line, block_count * free_quantity)
        return {
            'base_paid': base_paid,
            'extra_paid': extra_paid,
            'free': free,
            }


class PromotionNxmLine(ModelSQL, ModelView):
    __name__ = 'sale.promotion.nxm.line'

    promotion = fields.Many2One(
        'sale.promotion', "Promotion", required=True, ondelete='CASCADE')
    unit = fields.Function(
        fields.Many2One('product.uom', "Unit"), 'on_change_with_unit')
    product = fields.Many2One(
        'product.product', "Product", required=True, ondelete='CASCADE',
        context={
            'company': Eval('_parent_promotion', {}).get('company', -1),
            },
        depends={'promotion'})
    default_package = fields.Many2One(
        'product.package', "Default Package",
        domain=[
            ['OR',
                ('template', '=', Eval('product_template', 0)),
                ('product', '=', Eval('product', 0)),
            ],
        ],
        depends=['product', 'product_template'])
    product_template = fields.Function(
        fields.Many2One('product.template', "Product Template"),
        'on_change_with_product_template')
    quantity = fields.Float(
        "Quantity", digits='unit',
        domain=[('quantity', '>=', 0)])
    package_quantity = fields.Float(
        "Package Quantity", digits='unit',
        domain=[('package_quantity', '>=', 0)])
    free_quantity = fields.Float(
        "Free Quantity", digits='unit',
        domain=[('free_quantity', '>=', 0)])
    free_package_quantity = fields.Float(
        "Free Package Quantity", digits='unit',
        domain=[('free_package_quantity', '>=', 0)])

    @fields.depends('promotion', '_parent_promotion.unit')
    def on_change_with_unit(self, name=None):
        if self.promotion and self.promotion.unit:
            return self.promotion.unit.id

    @fields.depends('product')
    def on_change_with_product_template(self, name=None):
        if self.product:
            return self.product.template.id

    def _get_product_default_package(self):
        if not self.product:
            return
        Package = Pool().get('product.package')
        packages = Package.search([
                ('product', '=', self.product.id),
                ], order=[('is_default', 'DESC'), ('id', 'ASC')], limit=1)
        if packages:
            return packages[0]
        packages = Package.search([
                ('template', '=', self.product.template.id),
                ('product', '=', None),
                ], order=[('is_default', 'DESC'), ('id', 'ASC')], limit=1)
        if packages:
            return packages[0]

    @fields.depends(
        'product', 'default_package', 'package_quantity',
        'free_package_quantity')
    def on_change_product(self):
        if not self.product:
            self.default_package = None
            self.quantity = None
            self.free_quantity = None
            return
        self.default_package = self._get_product_default_package()
        self._sync_package_quantities()

    @fields.depends(
        'default_package', 'package_quantity', 'free_package_quantity')
    def on_change_default_package(self):
        self._sync_package_quantities()

    @fields.depends(
        'default_package', 'package_quantity', 'free_package_quantity')
    def on_change_package_quantity(self):
        self._sync_package_quantities()

    @fields.depends(
        'default_package', 'package_quantity', 'free_package_quantity')
    def on_change_free_package_quantity(self):
        self._sync_package_quantities()

    def _sync_package_quantities(self):
        if not self.default_package or not self.default_package.quantity:
            return
        self.quantity = (
            (self.package_quantity or 0) * self.default_package.quantity)
        self.free_quantity = (
            (self.free_package_quantity or 0) * self.default_package.quantity)


class SaleLine(metaclass=PoolMeta):
    __name__ = 'sale.line'

    nxm_generated = fields.Boolean(
        "N+M Generated",
        states={
            'invisible': True,
            })
    nxm_parent_line = fields.Many2One(
        'sale.line', "N+M Parent Line", ondelete='CASCADE',
        states={
            'invisible': True,
            })
    nxm_requested_quantity = fields.Float(
        "Requested Quantity", digits='unit',
        states={
            'invisible': ((Eval('type') != 'line')
                | Eval('nxm_generated', False)
                | ~Bool(Eval('product'))),
            'readonly': Eval('sale_state') != 'draft',
            },
        help=(
            "Original quantity requested by the customer before the "
            "promotion splits the line."
            ))

    @classmethod
    def __setup__(cls):
        super().__setup__()
        for field in [cls.product, cls.quantity, cls.unit, cls.unit_price]:
            readonly = field.states.get('readonly')
            if readonly is not None:
                field.states['readonly'] = readonly | Eval('nxm_generated', False)
            else:
                field.states['readonly'] = Eval('nxm_generated', False)

    @classmethod
    def create(cls, vlist):
        vlist = [cls._set_original_unit_price(values.copy()) for values in vlist]
        return super().create(vlist)

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        to_write = []
        for lines, values in zip(actions, actions):
            to_write.extend((lines, cls._set_original_unit_price(values.copy())))
        super().write(*to_write)

    @staticmethod
    def _set_original_unit_price(values):
        if values.get('promotion') and not values.get('original_unit_price'):
            original_unit_price = values.get('base_price', values.get('unit_price'))
            if original_unit_price is not None:
                values['original_unit_price'] = original_unit_price
        return values

    @fields.depends(
        'product', 'quantity', 'unit', 'sale', '_parent_sale.company',
        '_parent_sale.sale_date', '_parent_sale.price_list',
        methods=['compute_unit_price'])
    def get_nxm_promotion(self, requested_quantity=None, promotions=None):
        Promotion = Pool().get('sale.promotion')
        if not self.sale or not self.product or not self.unit:
            return
        return Promotion.get_nxm_promotion(
            self.sale, self, requested_quantity=requested_quantity,
            promotions=promotions)

    @fields.depends(
        'product', 'quantity', 'nxm_generated',
        methods=['compute_unit_price', 'on_change_with_amount',
            'on_change_with_discount_rate', 'on_change_with_discount_amount',
            'on_change_with_discount'])
    def on_change_quantity(self):
        super().on_change_quantity()
        if not self.nxm_generated:
            self.nxm_requested_quantity = self.quantity

    @fields.depends(
        'product', 'quantity', 'nxm_generated', 'nxm_requested_quantity',
        methods=['compute_unit_price', 'on_change_with_amount'])
    def on_change_nxm_requested_quantity(self):
        if self.nxm_generated or self.nxm_requested_quantity is None:
            return
        self.quantity = self.nxm_requested_quantity
        if self.product:
            self.unit_price = self.compute_unit_price()
        self.amount = self.on_change_with_amount()

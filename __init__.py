# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool

from . import sale


def register():
    Pool.register(
        sale.Promotion,
        sale.PromotionNxmLine,
        sale.Sale,
        sale.SaleLine,
        module='sale_nxm_promotion', type_='model')

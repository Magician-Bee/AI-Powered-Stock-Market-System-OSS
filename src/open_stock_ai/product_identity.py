"""Official product categories shared by source parsing and entry admission.

Pure constants: safe to import during data-platform and execution startup.
"""

OFFICIAL_PRODUCT_SECTIONS = {
    "股票": ("ordinary_stock", ("ES",), "ordinary"),
    "創新板": ("ordinary_stock", ("ES",), "innovation"),
    "興櫃": ("ordinary_stock", ("ES",), "emerging"),
    "特別股": ("preferred_stock", ("EP", "EF"), "other"),
    "ETF": ("etf", ("CE",), "other"),
    "ETN": ("etn", ("CM",), "other"),
    "臺灣存託憑證(TDR)": ("depositary_receipt", ("ED",), "other"),
    "上市認購(售)權證": ("warrant", ("RW",), "other"),
    "上櫃認購(售)權證": ("warrant", ("RW",), "other"),
    "受益證券-不動產投資信託": ("other", ("CB",), "other"),
    "受益證券-資產基礎證券": ("other", ("DA",), "other"),
}

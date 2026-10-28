## 2.2 — 2026-10-28

- Medical/health/certification wording no longer excludes products or images in the full catalog policy.
- All three brands export to one ALL workbook. Missing origin/images/category facts stay blank or flagged in needs_data.csv instead of silently dropping products.
- A separate COMPLETE_DATA workbook contains the subset without missing required data; no Temu approval guarantee.
- Eco image/text checks remain enabled. Original SKU and exact registered entities preserved.
- Summary includes actual exported counts by brand.

# 2.1 — 28.10.2026

- Фактически заглавия и описания вместо отхвърляне на целия продукт за рекламния текст на сайта.
- Отделни проверки на медицински, екологични и общи здравни твърдения върху реалните опаковки.
- Конкретни доказателства за произход по SKU от официални продуктови страници.
- Визуални решения за непроменени снимки по SHA-256.
- Поправено TSV разчитане на OCR резултатите и версия на кеша.
- Поправена категория за мио-инозитол и разпознаване на B-комплекс.
- Запазени оригинални SKU, три категории, новият Temu шаблон и потвърдените фирмени данни.
- Режим `--from-results` за обработка на съществуващ резултат.

Подготвеният Excel съдържа 8 проверени продукта. Останалите са отделени за проверка, не са обявени за автоматично допустими.

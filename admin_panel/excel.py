"""Сборка XLSX-файлов с BOM-форматированием для отчётов админки."""
import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


HEADER_FILL = PatternFill(start_color='E8EEF7', end_color='E8EEF7', fill_type='solid')
HEADER_FONT = Font(bold=True, size=11)
TOTAL_FONT = Font(bold=True, size=11)
TOTAL_FILL = PatternFill(start_color='F2F6FB', end_color='F2F6FB', fill_type='solid')
THIN = Side(border_style='thin', color='CCCCCC')
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def build_xlsx(header, rows, total_row=None, sheet_name='Отчёт', title=None,
               column_widths=None):
    """
    header: список строк — заголовки колонок
    rows: список списков — данные
    total_row: список (по длине header) — итоговая строка, опционально
    column_widths: список ширин в символах, опционально
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31] or 'Отчёт'

    row_offset = 0

    if title:
        ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
        row_offset = 2

    # Заголовки
    header_row = row_offset + 1
    for col_idx, cell_val in enumerate(header, start=1):
        c = ws.cell(row=header_row, column=col_idx, value=cell_val)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.border = BORDER
        c.alignment = Alignment(vertical='center', horizontal='left', wrap_text=True)

    # Данные
    for i, row in enumerate(rows, start=header_row + 1):
        for col_idx, val in enumerate(row, start=1):
            c = ws.cell(row=i, column=col_idx, value=val)
            c.border = BORDER
            c.alignment = Alignment(vertical='top', wrap_text=False)

    # Итоговая строка
    if total_row is not None:
        total_idx = header_row + 1 + len(rows)
        for col_idx, val in enumerate(total_row, start=1):
            c = ws.cell(row=total_idx, column=col_idx, value=val)
            c.font = TOTAL_FONT
            c.fill = TOTAL_FILL
            c.border = BORDER

    # Ширины колонок
    if column_widths:
        for i, w in enumerate(column_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
    else:
        # автоширина по содержимому, max 50
        for i in range(1, len(header) + 1):
            max_len = len(str(header[i - 1]))
            for r in rows[:200]:
                if i - 1 < len(r):
                    max_len = max(max_len, len(str(r[i - 1])))
            ws.column_dimensions[get_column_letter(i)].width = min(max_len + 3, 50)

    # Закрепить шапку
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    # Автофильтр
    ws.auto_filter.ref = (
        f'A{header_row}:{get_column_letter(len(header))}{header_row + len(rows)}'
    )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def xlsx_response(data, filename):
    from django.http import HttpResponse
    response = HttpResponse(
        data,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

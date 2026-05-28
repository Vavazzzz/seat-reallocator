"""
Seat Reallocator — desktop GUI (customtkinter)
"""
import os
import queue
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

# ── PyInstaller: add bundled CBC binary directory to PATH ──────────────────
if getattr(sys, 'frozen', False):
    _cbc_dir = Path(sys._MEIPASS) / 'pulp' / 'solverdir' / 'cbc' / 'win' / 'i64'
    os.environ['PATH'] = str(_cbc_dir) + os.pathsep + os.environ.get('PATH', '')

import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

ctk.set_appearance_mode('System')
ctk.set_default_color_theme('blue')


# ──────────────────────────────────────────────────────────────────────────────
# App window
# ──────────────────────────────────────────────────────────────────────────────

class App(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title('Seat Reallocator')
        self.geometry('740x580')
        self.minsize(680, 520)

        self._q: queue.Queue = queue.Queue()
        self._build_ui()
        self._poll()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color='transparent')
        hdr.pack(fill='x', padx=24, pady=(20, 0))
        ctk.CTkLabel(hdr, text='Seat Reallocator',
                     font=ctk.CTkFont(size=20, weight='bold')).pack(anchor='w')
        ctk.CTkLabel(hdr, text='Genera report di riallocazione posti per eventi concertistici',
                     text_color='gray50', font=ctk.CTkFont(size=12)).pack(anchor='w')

        # Tabs
        self.tabs = ctk.CTkTabview(self, height=280)
        self.tabs.pack(fill='x', padx=24, pady=(16, 0))

        for name in ('Riallocazione', 'Capofila', 'Post-Report', 'Riallocazioni', 'Riallocazioni 2', 'Export Pubblici'):
            self.tabs.add(name)

        self._v_rall  = self._tab_reallocation()
        self._v_cap   = self._tab_capofila()
        self._v_post  = self._tab_post()
        self._v_swap  = self._tab_swap()
        self._v_rall2 = self._tab_reallocation_report()
        self._v_exp   = self._tab_export()

        # Generate button
        self.btn = ctk.CTkButton(
            self, text='Genera Report', height=42,
            font=ctk.CTkFont(size=14, weight='bold'),
            command=self._on_generate,
        )
        self.btn.pack(fill='x', padx=24, pady=(20, 8))

        # Progress
        self.progress = ctk.CTkProgressBar(self, mode='indeterminate', height=6)
        self.progress.pack(fill='x', padx=24)
        self.progress.set(0)

        # Status
        self.status = ctk.CTkLabel(
            self, text='', text_color='gray50',
            font=ctk.CTkFont(size=12), wraplength=690, justify='left',
        )
        self.status.pack(fill='x', padx=24, pady=(10, 20))

    # ── Tab builders ──────────────────────────────────────────────────────

    def _file_row(self, parent, label, filetypes, optional=False, pady=5):
        """File picker row. Returns a StringVar holding the full path."""
        frame = ctk.CTkFrame(parent, fg_color='transparent')
        frame.pack(fill='x', pady=pady)

        suffix = ' (opzionale)' if optional else ' *'
        ctk.CTkLabel(frame, text=label + suffix,
                     width=210, anchor='w',
                     font=ctk.CTkFont(size=12)).pack(side='left')

        path_var = tk.StringVar()

        entry = ctk.CTkEntry(frame, width=310, state='disabled',
                             fg_color=('gray90', 'gray20'))
        entry.pack(side='left', padx=(8, 6))

        def _browse():
            path = filedialog.askopenfilename(
                filetypes=filetypes, title=f'Seleziona {label}',
            )
            if path:
                path_var.set(path)
                entry.configure(state='normal')
                entry.delete(0, 'end')
                entry.insert(0, Path(path).name)
                entry.configure(state='disabled')

        ctk.CTkButton(frame, text='Sfoglia', width=80, height=28,
                      command=_browse).pack(side='left')
        return path_var

    def _hint(self, parent, text):
        ctk.CTkLabel(parent, text=text, text_color='gray50',
                     font=ctk.CTkFont(size=11)).pack(anchor='w', pady=(2, 8))

    def _tab_reallocation(self):
        t = self.tabs.tab('Riallocazione')
        self._hint(t, 'Step 1 — Dal CSV grezzo; produce report_annotated.xlsx')
        csv = self._file_row(t, 'Report CSV',  [('CSV', '*.csv')])
        txt = self._file_row(t, 'Orders TXT', [('TXT', '*.txt')], optional=True)
        return {'csv': csv, 'orders': txt}

    def _tab_capofila(self):
        t = self.tabs.tab('Capofila')
        self._hint(t, 'Step 2 — Correzione posti corsia L/R; produce report_capofila.xlsx')
        xlsx = self._file_row(t, 'Report annotato XLSX', [('Excel', '*.xlsx')])
        return {'xlsx': xlsx}

    def _tab_post(self):
        t = self.tabs.tab('Post-Report')
        self._hint(t, 'Step 3 — Merge DF1+DF2+DF3 con filtro data annullo; produce post_report.xlsx')
        ann = self._file_row(t, 'Annotato XLSX  (DF1)', [('Excel', '*.xlsx')], pady=4)
        upd = self._file_row(t, 'CSV aggiornato (DF2)', [('CSV', '*.csv')],   pady=4)
        ext = self._file_row(t, 'Dati suppl.    (DF3)', [('CSV', '*.csv')],   pady=4)

        row = ctk.CTkFrame(t, fg_color='transparent')
        row.pack(fill='x', pady=4)
        ctk.CTkLabel(row, text='Data annullo da *', width=210, anchor='w',
                     font=ctk.CTkFont(size=12)).pack(side='left')
        date_var = tk.StringVar()
        ctk.CTkEntry(row, textvariable=date_var, width=160,
                     placeholder_text='YYYY-MM-DD').pack(side='left', padx=(8, 0))

        return {'ann': ann, 'upd': upd, 'ext': ext, 'date': date_var}

    def _tab_swap(self):
        t = self.tabs.tab('Riallocazioni')
        self._hint(t, 'Extra — Mappatura per-posto vecchio→nuovo ordine; produce swap_report.xlsx')
        xlsx = self._file_row(t, 'Source XLSX', [('Excel', '*.xlsx')])
        return {'xlsx': xlsx}

    def _tab_reallocation_report(self):
        t = self.tabs.tab('Riallocazioni 2')
        self._hint(t, 'Extra — Report per-posto con ordine, nomi e stato; produce reallocation_report.xlsx')
        xlsx = self._file_row(t, 'Source XLSX', [('Excel', '*.xlsx')])
        return {'xlsx': xlsx}

    def _tab_export(self):
        t = self.tabs.tab('Export Pubblici')
        self._hint(t, 'Extra — File per-evento × n° biglietti; produce uno ZIP con tutti i file')
        xlsx = self._file_row(t, 'Source XLSX', [('Excel', '*.xlsx')])
        return {'xlsx': xlsx}

    # ── Generate ──────────────────────────────────────────────────────────

    def _on_generate(self):
        tab = self.tabs.get()
        try:
            args = self._collect(tab)
        except ValueError as exc:
            self._set_status(str(exc), color='#e05555')
            return

        self.btn.configure(state='disabled')
        self.progress.start()
        self._set_status('Elaborazione in corso…')
        threading.Thread(target=self._run, args=(tab, args), daemon=True).start()

    def _collect(self, tab):
        def req(var, name):
            v = var.get().strip()
            if not v:
                raise ValueError(f'Selezionare il file: {name}')
            return v

        if tab == 'Riallocazione':
            return {
                'csv':    req(self._v_rall['csv'], 'Report CSV'),
                'orders': self._v_rall['orders'].get().strip() or None,
            }
        if tab == 'Capofila':
            return {'xlsx': req(self._v_cap['xlsx'], 'Report annotato XLSX')}
        if tab == 'Post-Report':
            date = self._v_post['date'].get().strip()
            if not date:
                raise ValueError('Inserire la data annullo (YYYY-MM-DD)')
            return {
                'ann':  req(self._v_post['ann'], 'Annotato XLSX (DF1)'),
                'upd':  req(self._v_post['upd'], 'CSV aggiornato (DF2)'),
                'ext':  req(self._v_post['ext'], 'Dati supplementari CSV (DF3)'),
                'date': date,
            }
        if tab == 'Riallocazioni':
            return {'xlsx': req(self._v_swap['xlsx'], 'Source XLSX')}
        if tab == 'Riallocazioni 2':
            return {'xlsx': req(self._v_rall2['xlsx'], 'Source XLSX')}
        if tab == 'Export Pubblici':
            return {'xlsx': req(self._v_exp['xlsx'], 'Source XLSX')}
        raise ValueError(f'Tab sconosciuto: {tab}')

    # ── Background thread ────────────────────────────────────────────────

    def _run(self, tab, args):
        temp_dir = tempfile.mkdtemp(prefix='sra_')
        try:
            out = self._dispatch(tab, args, temp_dir)
            self._q.put(('ok', out, temp_dir))
        except BaseException as exc:
            msg = str(exc.code) if isinstance(exc, SystemExit) and exc.code else str(exc)
            self._q.put(('err', msg or 'Errore sconosciuto', temp_dir))

    def _dispatch(self, tab, args, temp_dir):
        if tab == 'Riallocazione':    return self._do_reallocation(args, temp_dir)
        if tab == 'Capofila':         return self._do_capofila(args, temp_dir)
        if tab == 'Post-Report':      return self._do_post(args, temp_dir)
        if tab == 'Riallocazioni':    return self._do_swap(args, temp_dir)
        if tab == 'Riallocazioni 2':  return self._do_rall2(args, temp_dir)
        if tab == 'Export Pubblici':  return self._do_export(args, temp_dir)

    # ── Business logic ───────────────────────────────────────────────────

    def _do_reallocation(self, args, temp_dir):
        from seat_reallocator.config import OCCUPIED
        from seat_reallocator.engine import (
            detect_collateral, detect_non_consecutive_orders, process_event,
        )
        from seat_reallocator.io import load_tickets, parse_orders
        from seat_reallocator.reporter import write_full_report

        out = os.path.join(temp_dir, 'report_annotated.xlsx')
        tickets = load_tickets(args['csv'])
        orders_by_event = (
            parse_orders(args['orders']) if args['orders']
            else detect_non_consecutive_orders(tickets)
        )
        all_moves, all_infeasible = [], []
        for event_date, event_df in tickets.groupby('Data evento'):
            problematic = orders_by_event.get(event_date, set())
            if not problematic:
                continue
            moves, infeasible = process_event(event_df, problematic)
            for m in moves:
                m['Data evento'] = event_date
            all_moves.extend(moves)
            all_infeasible.extend((event_date, oid) for oid in infeasible)

        active = tickets[tickets['Stato posto'].isin(OCCUPIED)]
        infeasible_set = {(ed, oid) for ed, oid in all_infeasible}
        collateral = detect_collateral(active, all_moves, infeasible_set)
        write_full_report(args['csv'], all_moves, infeasible_set, collateral, path=out)
        return out

    def _do_capofila(self, args, temp_dir):
        from seat_reallocator.capofila import build_occupied_current, fix_capofila_orders
        from seat_reallocator.engine import detect_non_consecutive_orders
        from seat_reallocator.io import load_tickets
        from seat_reallocator.reporter import write_full_report

        out = os.path.join(temp_dir, 'report_capofila.xlsx')
        tickets = load_tickets(args['xlsx'])
        orders_by_event = detect_non_consecutive_orders(tickets)
        all_moves, all_infeasible = [], []
        for event_date, event_df in tickets.groupby('Data evento'):
            problematic = orders_by_event.get(event_date, set())
            if not problematic:
                continue
            capofila_orders = [
                oid for oid in problematic
                if not event_df[
                    (event_df['Codice ordine'] == str(oid))
                    & event_df['Settore prezzi'].str.contains('capofila', case=False, na=False)
                ].empty
            ]
            if not capofila_orders:
                continue
            occupied = build_occupied_current(event_df)
            moves, still_inf = fix_capofila_orders(
                event_df, capofila_orders, occupied, event_date,
            )
            all_moves.extend(moves)
            all_infeasible.extend((event_date, oid) for oid in still_inf)

        infeasible_set = {(ed, oid) for ed, oid in all_infeasible}
        write_full_report(args['xlsx'], all_moves, infeasible_set, [], path=out)
        return out

    def _do_post(self, args, temp_dir):
        from seat_reallocator.post_report import build
        out = os.path.join(temp_dir, 'post_report.xlsx')
        build(args['ann'], args['upd'], args['ext'], args['date'], out)
        return out

    def _do_swap(self, args, temp_dir):
        from seat_reallocator.reallocations_report import build
        out = os.path.join(temp_dir, 'swap_report.xlsx')
        build(args['xlsx'], out)
        return out

    def _do_rall2(self, args, temp_dir):
        from seat_reallocator.reallocation_report import build_reallocation_report
        out = os.path.join(temp_dir, 'reallocation_report.xlsx')
        build_reallocation_report(Path(args['xlsx']), Path(out))
        return out

    def _do_export(self, args, temp_dir):
        from seat_reallocator.exporter import export_swap_files
        out_dir = Path(temp_dir) / 'export'
        files = export_swap_files(Path(args['xlsx']), out_dir)
        if not files:
            raise RuntimeError('Nessun ordine SPOSTATO trovato nel file.')
        zip_path = os.path.join(temp_dir, 'export_pubblici.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, Path(f).name)
        return zip_path

    # ── Queue polling & save dialog ──────────────────────────────────────

    def _poll(self):
        try:
            while True:
                kind, data, temp_dir = self._q.get_nowait()
                self.progress.stop()
                self.progress.set(0)
                self.btn.configure(state='normal')
                if kind == 'ok':
                    self._on_done(data, temp_dir)
                else:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    self._set_status(f'Errore: {data}', color='#e05555')
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_done(self, output_path, temp_dir):
        is_zip = Path(output_path).suffix.lower() == '.zip'
        if is_zip:
            save_path = filedialog.asksaveasfilename(
                title='Salva ZIP',
                defaultextension='.zip',
                filetypes=[('ZIP', '*.zip')],
                initialfile='export_pubblici.zip',
            )
        else:
            save_path = filedialog.asksaveasfilename(
                title='Salva report',
                defaultextension='.xlsx',
                filetypes=[('Excel', '*.xlsx')],
                initialfile=Path(output_path).name,
            )

        if save_path:
            shutil.copy2(output_path, save_path)
            self._set_status(f'✓ Salvato: {save_path}', color='#2a9d4e')
        else:
            self._set_status('Salvataggio annullato.', color='gray50')

        shutil.rmtree(temp_dir, ignore_errors=True)

    def _set_status(self, text, color='gray50'):
        self.status.configure(text=text, text_color=color)


# ──────────────────────────────────────────────────────────────────────────────

def main():
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()

import io
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB upload limit

# In-memory job store: {job_id: {status, output_path, temp_dir, error, created_at}}
_jobs: dict = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 1800  # 30 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _cleanup_old_jobs() -> None:
    cutoff = time.time() - _JOB_TTL
    stale_dirs = []
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j['created_at'] < cutoff]
        for jid in stale:
            td = _jobs.pop(jid).get('temp_dir')
            if td:
                stale_dirs.append(td)
    for d in stale_dirs:
        shutil.rmtree(d, ignore_errors=True)


def _exc_message(exc: BaseException) -> str:
    if isinstance(exc, SystemExit):
        return str(exc.code) if exc.code is not None else 'Processing failed'
    return str(exc) or type(exc).__name__


# ---------------------------------------------------------------------------
# Job runners (executed in daemon threads)
# ---------------------------------------------------------------------------

def _run_reallocation(job_id: str, temp_dir: str, csv_path: str, orders_path: str | None) -> None:
    try:
        from seat_reallocator.config import OCCUPIED
        from seat_reallocator.engine import (
            detect_collateral,
            detect_non_consecutive_orders,
            process_event,
        )
        from seat_reallocator.io import load_tickets, parse_orders
        from seat_reallocator.reporter import write_full_report

        output_path = os.path.join(temp_dir, 'report_annotated.xlsx')

        tickets = load_tickets(csv_path)
        orders_by_event = (
            parse_orders(orders_path) if orders_path
            else detect_non_consecutive_orders(tickets)
        )

        all_moves: list = []
        all_infeasible: list = []

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
        collateral_rows = detect_collateral(active, all_moves, infeasible_set)
        write_full_report(csv_path, all_moves, infeasible_set, collateral_rows, path=output_path)

        _set_job(job_id, status='complete', output_path=output_path)
        logger.info('Reallocation job %s complete', job_id)

    except BaseException as exc:
        logger.exception('Reallocation job %s failed', job_id)
        _set_job(job_id, status='error', error=_exc_message(exc))


def _run_capofila(job_id: str, temp_dir: str, xlsx_path: str) -> None:
    try:
        from seat_reallocator.capofila import build_occupied_current, fix_capofila_orders
        from seat_reallocator.engine import detect_non_consecutive_orders
        from seat_reallocator.io import load_tickets
        from seat_reallocator.reporter import write_full_report

        output_path = os.path.join(temp_dir, 'report_capofila.xlsx')

        tickets = load_tickets(xlsx_path)
        orders_by_event = detect_non_consecutive_orders(tickets)

        all_moves: list = []
        all_infeasible: list = []

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
            moves, still_infeasible = fix_capofila_orders(
                event_df, capofila_orders, occupied, event_date,
            )
            all_moves.extend(moves)
            all_infeasible.extend((event_date, oid) for oid in still_infeasible)

        infeasible_set = {(ed, oid) for ed, oid in all_infeasible}
        write_full_report(xlsx_path, all_moves, infeasible_set, [], path=output_path)

        _set_job(job_id, status='complete', output_path=output_path)
        logger.info('Capofila job %s complete', job_id)

    except BaseException as exc:
        logger.exception('Capofila job %s failed', job_id)
        _set_job(job_id, status='error', error=_exc_message(exc))


def _run_post_report(
    job_id: str,
    temp_dir: str,
    annotated_path: str,
    updated_path: str,
    extra_path: str,
    annullo_from: str,
) -> None:
    try:
        from seat_reallocator.post_report import build

        output_path = os.path.join(temp_dir, 'post_report.xlsx')
        build(annotated_path, updated_path, extra_path, annullo_from, output_path)

        _set_job(job_id, status='complete', output_path=output_path)
        logger.info('Post-report job %s complete', job_id)

    except BaseException as exc:
        logger.exception('Post-report job %s failed', job_id)
        _set_job(job_id, status='error', error=_exc_message(exc))


def _run_swap_report(job_id: str, temp_dir: str, xlsx_path: str) -> None:
    try:
        from seat_reallocator.reallocations_report import build

        output_path = os.path.join(temp_dir, 'swap_report.xlsx')
        build(xlsx_path, output_path)

        _set_job(job_id, status='complete', output_path=output_path)
        logger.info('Swap-report job %s complete', job_id)

    except BaseException as exc:
        logger.exception('Swap-report job %s failed', job_id)
        _set_job(job_id, status='error', error=_exc_message(exc))


def _run_export_pubblici(job_id: str, temp_dir: str, xlsx_path: str) -> None:
    try:
        from seat_reallocator.exporter import export_swap_files

        out_dir = Path(temp_dir) / 'export'
        files_written = export_swap_files(Path(xlsx_path), out_dir)

        if not files_written:
            _set_job(job_id, status='error', error='Nessun ordine SPOSTATO trovato nel file.')
            return

        zip_path = os.path.join(temp_dir, 'export_pubblici.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in files_written:
                zf.write(f, Path(f).name)

        _set_job(job_id, status='complete', output_path=zip_path)
        logger.info('Export-pubblici job %s complete (%d files)', job_id, len(files_written))

    except BaseException as exc:
        logger.exception('Export-pubblici job %s failed', job_id)
        _set_job(job_id, status='error', error=_exc_message(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/generate', methods=['POST'])
def generate():
    _cleanup_old_jobs()

    report_type = request.form.get('report_type', '')
    job_id = str(uuid.uuid4())
    temp_dir = tempfile.mkdtemp(prefix='sra_')

    def _abort(msg: str, code: int = 400):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({'error': msg}), code

    def _save(field: str, stem: str) -> str | None:
        f = request.files.get(field)
        if not f or not f.filename:
            return None
        ext = Path(f.filename).suffix.lower() or ''
        path = os.path.join(temp_dir, stem + ext)
        f.save(path)
        return path

    if report_type == 'reallocation':
        csv_path = _save('report_csv', 'report')
        if not csv_path:
            return _abort('Selezionare il file CSV del report.')
        orders_path = _save('orders_txt', 'orders')
        target, args = _run_reallocation, (job_id, temp_dir, csv_path, orders_path)

    elif report_type == 'capofila':
        xlsx_path = _save('annotated_xlsx', 'report_annotated')
        if not xlsx_path:
            return _abort('Selezionare il file XLSX annotato.')
        target, args = _run_capofila, (job_id, temp_dir, xlsx_path)

    elif report_type == 'post':
        ann_path = _save('annotated_xlsx', 'report_annotated')
        upd_path = _save('updated_csv', 'updated_report')
        ext_path = _save('extra_csv', 'extra')
        annullo_from = request.form.get('annullo_from', '').strip()
        if not ann_path or not upd_path or not ext_path or not annullo_from:
            return _abort('Tutti i file e la data sono obbligatori.')
        target, args = _run_post_report, (job_id, temp_dir, ann_path, upd_path, ext_path, annullo_from)

    elif report_type == 'swap':
        xlsx_path = _save('source_xlsx', 'source')
        if not xlsx_path:
            return _abort('Selezionare il file XLSX sorgente.')
        target, args = _run_swap_report, (job_id, temp_dir, xlsx_path)

    elif report_type == 'export_pubblici':
        xlsx_path = _save('source_xlsx', 'source')
        if not xlsx_path:
            return _abort('Selezionare il file XLSX sorgente.')
        target, args = _run_export_pubblici, (job_id, temp_dir, xlsx_path)

    else:
        return _abort('Tipo di report non valido.')

    with _jobs_lock:
        _jobs[job_id] = {
            'status': 'pending',
            'output_path': None,
            'temp_dir': temp_dir,
            'error': None,
            'created_at': time.time(),
        }

    threading.Thread(target=target, args=args, daemon=True).start()
    logger.info('Started %s job %s', report_type, job_id)
    return jsonify({'job_id': job_id})


@app.route('/api/status/<job_id>')
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job non trovato'}), 404
    return jsonify({'status': job['status'], 'error': job.get('error')})


@app.route('/api/download/<job_id>')
def download(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job non trovato'}), 404
    if job['status'] != 'complete':
        return jsonify({'error': 'File non ancora pronto'}), 202

    output_path = job['output_path']
    filename = os.path.basename(output_path)
    temp_dir = job['temp_dir']

    # Read into memory so the temp dir can be removed before streaming
    with open(output_path, 'rb') as fh:
        data = io.BytesIO(fh.read())

    shutil.rmtree(temp_dir, ignore_errors=True)
    with _jobs_lock:
        _jobs.pop(job_id, None)

    ext = Path(filename).suffix.lower()
    mimetype = (
        'application/zip'
        if ext == '.zip'
        else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

    data.seek(0)
    return send_file(
        data,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype,
    )


@app.errorhandler(413)
def too_large(_e):
    return jsonify({'error': 'Il file supera il limite di 50 MB'}), 413


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

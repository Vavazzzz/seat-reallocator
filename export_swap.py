"""
export_swap.py — transform report_annotated.xlsx into per-event, per-ticket-count swap files.

Usage:
    python export_swap.py [input.xlsx] [output_dir]

Defaults:
    input  : data/report_annotated.xlsx
    output : swap_output/
"""
import sys
from pathlib import Path

import pandas as pd


INPUT_DEFAULT = Path("data/report_annotated.xlsx")
OUTPUT_DEFAULT = Path("swap_output")
SKIP_SHEETS = {"COLLATERALE"}


def _safe(val):
    """Return val unchanged; NaN → empty string for Excel cleanliness."""
    if pd.isna(val):
        return ""
    return val


def pivot_order(group: pd.DataFrame) -> dict:
    """Convert all rows for one order into a single wide dict."""
    group = group.sort_values("Posto").reset_index(drop=True)
    first = group.iloc[0]

    row: dict = {
        "ordine": _safe(first["Codice ordine"]),
        "cognome": _safe(first["Cognome"]),
        "nome":    _safe(first["Nome"]),
        "email":   _safe(first["Email"]),
    }

    for i, (_, tix) in enumerate(group.iterrows(), start=1):
        n = f"{i:02d}"
        moved = str(tix.get("Stato", "")).strip().upper() == "SPOSTATO"
        nuovo_posto = tix["Nuovo posto"] if moved else tix["Posto"]

        row[f"cognome_{n}"]       = _safe(tix.get("Cognome partecipante", ""))
        row[f"nome_{n}"]          = _safe(tix.get("Nome partecipante", ""))
        row[f"barcode_{n}"]       = _safe(tix.get("Codice supporto", ""))
        row[f"vecchio_settore_{n}"] = _safe(tix.get("Item", ""))
        row[f"vecchio_blocco_{n}"]  = _safe(tix.get("Settore", ""))
        row[f"vecchia_fila_{n}"]    = _safe(tix.get("Fila", ""))
        row[f"vecchio_posto_{n}"]   = _safe(tix.get("Posto", ""))
        row[f"nuovo_settore_{n}"]  = _safe(tix.get("Item", ""))
        row[f"nuovo_blocco_{n}"]   = _safe(tix.get("Settore", ""))
        row[f"nuova_fila_{n}"]     = _safe(tix.get("Fila", ""))
        row[f"nuovo_posto_{n}"]    = _safe(nuovo_posto)

    return row


def process_sheet(df: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """
    Process one event sheet. Returns a dict keyed by ticket-count,
    each value is a DataFrame of wide rows for orders with that many tickets.
    """
    # Only keep orders that have at least one SPOSTATO seat.
    moved_orders = df.loc[
        df["Stato"].str.strip().str.upper() == "SPOSTATO", "Codice ordine"
    ].unique()

    if len(moved_orders) == 0:
        return {}

    relevant = df[df["Codice ordine"].isin(moved_orders)]
    rows = [pivot_order(grp) for _, grp in relevant.groupby("Codice ordine", sort=False)]

    result: dict[int, list[dict]] = {}
    for row in rows:
        # count ticket slots: number of keys matching vecchio_posto_NN
        n_tix = sum(1 for k in row if k.startswith("vecchio_posto_"))
        result.setdefault(n_tix, []).append(row)

    return {n: pd.DataFrame(rows_list) for n, rows_list in result.items()}


def main(input_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    xl = pd.ExcelFile(input_path)
    files_written: list[str] = []

    for sheet in xl.sheet_names:
        if sheet.strip().upper() in SKIP_SHEETS:
            continue

        df = xl.parse(sheet, dtype=str)
        buckets = process_sheet(df)

        if not buckets:
            print(f"  [{sheet}] no reallocated orders — skipped")
            continue

        for n_tix, wide_df in sorted(buckets.items()):
            label = f"{n_tix}_ticket" + ("s" if n_tix != 1 else "")
            fname = f"{sheet}_{label}.xlsx"
            out_path = output_dir / fname
            wide_df.to_excel(out_path, index=False)
            files_written.append(str(out_path))
            print(f"  [{sheet}] {n_tix} ticket(s): {len(wide_df)} orders -> {out_path}")

    if files_written:
        print(f"\nDone. {len(files_written)} file(s) written to {output_dir}/")
    else:
        print("\nNo output files written.")


if __name__ == "__main__":
    args = sys.argv[1:]
    inp = Path(args[0]) if len(args) >= 1 else INPUT_DEFAULT
    out = Path(args[1]) if len(args) >= 2 else OUTPUT_DEFAULT

    if not inp.exists():
        sys.exit(f"Input file not found: {inp}")

    print(f"Reading {inp} …")
    main(inp, out)

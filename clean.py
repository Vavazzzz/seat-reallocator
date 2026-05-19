import argparse
import pandas as pd

parser = argparse.ArgumentParser()
parser.add_argument('input', nargs='?', default='report.csv')
args = parser.parse_args()

df = pd.read_csv(args.input, sep=';')

columns_to_keep = ['Codice ordine', 'Stato ordine', 'Stato posto', 'Data evento', 'Item', 'Settore', 'Fila', 'Posto', 'Settore prezzi', 'Selezione in mappa']

df_cleaned = df[columns_to_keep]
df_cleaned.to_csv('report_cleaned.csv', index=False)
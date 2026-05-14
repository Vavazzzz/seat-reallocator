import pandas as pd

df = pd.read_csv('report.csv', sep=';')

columns_to_keep = ['Codice ordine', 'Stato ordine', 'Stato posto', 'Data evento', 'Item', 'Settore', 'Fila', 'Posto', 'Settore prezzi', 'Selezione in mappa']

df_cleaned = df[columns_to_keep]
df_cleaned.to_csv('report_cleaned.csv', index=False)
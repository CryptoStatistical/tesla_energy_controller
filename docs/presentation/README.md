# Tesla Energy Controller Presentation

Materiali di presentazione del progetto:

Aggiornamento: **29 settembre 2026**, 16 slide. Contenuti allineati al codice del
commit `58372e6` (12 settembre 2026). L'HTML è il sorgente modificabile del PDF.

- [Presentazione tecnica PDF](tesla-energy-controller-presentation.pdf)
- [Presentazione tecnica HTML](tesla-energy-controller-presentation.html)
- [Anteprima HTML renderizzata](https://htmlpreview.github.io/?https://github.com/CryptoStatistical/tesla_energy_controller/blob/main/docs/presentation/tesla-energy-controller-presentation.html)

Nota: GitHub mostra i file HTML come sorgente. Il link di anteprima usa HTMLPreview per aprire la
presentazione come pagina renderizzata.

La revisione include l'avvio automatico nella finestra solare, il controllo Tesla
via BLE separato dalle misure Wall Connector, la ripresa dopo sospensione per quota
ALFA, la dashboard Tesla Remote Meter e la diagnostica admin. Aggiorna anche il
ruolo primario di SolarEdge web/cloud, la resilienza Wi-Fi e il supporto a una o tre
fasi. Corregge le precedenti affermazioni su funzionamento interamente offline,
cifratura dei collegamenti locali e avvio/arresto sempre manuali.

I grafici energetici sono esempi illustrativi, non misurazioni di un impianto.
L'eventuale collaborazione con Sinapsi è una proposta di sviluppo, non
un'integrazione ufficiale o una partnership già concordata.

Fonti della revisione: `README.md`, moduli `controller.py`, `web_runtime.py`,
`runtime.py`, `config.py`, dashboard, script di rete e relativi test. I conteggi
riportano 22 moduli Python, 213 casi di test raccolti in 11 file e 13 script.

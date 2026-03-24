**Slidescribe v3**

1. Refactor della CLI: lo script ora supporta configurazione ibrida con precedenza flag CLI > file .conf > default interni > prompt interattivo.
2. Aggiunta una gestione più strutturata dell’esecuzione, con supporto a modalità non interattiva, dry run, step selettivi/ripartenza da step intermedi e forcing dei checkpoint.
3. Migliorata la configurabilità della pipeline yt-dlp / LLM / logging, inclusi browser cookies, livelli di verbosità e prompt custom da file.
4. Espanse le sezioni --help e --manual per documentare uso, precedence e interazioni tra le opzioni.
5. Fix di compatibilità per macOS Bash 3.2, rimuovendo l’uso di local -n e adattando la costruzione del comando yt-dlp.

**Slidescribe v2**
1. Seconda versione funzionante della pipeline SlideScribe, con struttura base per estrazione slide, gestione prompt e generazione dell’output.
2. Migliorato il flusso di selezione ROI e raffinato progressivamente il prompt LLM, passando da una versione hardcoded a una formulazione più curata.
3. Aggiunto logging più chiaro per Screenshot_grabber e introdotta una prima gestione della verbosità da riga di comando.
4. Introdotto fallback per yt-dlp tramite installazione pipx in ~/.local/bin, con supporto aggiuntivo per impersonation.
5. Inclusi fix incrementali, correzioni di refusi e piccoli revert per stabilizzare il comportamento della pipeline.
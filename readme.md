**Slidescribe v2 --- workflow step by step**  
  
**Obiettivo**  
  
Trasformare un video di una lezione in due output finali:  
• un **PDF** con slide + testo associato  
• un **DOCX** con slide + testo associato  
  
La pipeline è pensata per essere robusta: non manda più direttamente l'SRT completo a un LLM, ma costruisce prima un formato intermedio **per slide**, molto più stabile.  
  
⸻  

Prerequisito — ambiente Python

Prima di eseguire la pipeline, va creato il virtual environment del progetto.

Il repository include:
	•	requirements.txt, che contiene le dipendenze Python necessarie
	•	create_venv.sh, che crea automaticamente la cartella .venv e installa i pacchetti richiesti

Dalla root del progetto eseguire:

./create_venv.sh

Se necessario:

chmod +x create_venv.sh
./create_venv.sh

Al termine, tutti gli script Python della pipeline vanno eseguiti usando l’interprete del progetto:

./.venv/bin/python

Questo approccio è preferibile rispetto a fare affidamento su source .venv/bin/activate, perché rende l’esecuzione più robusta e prevedibile anche in script e workflow automatizzati.

⸻ 
  
**Panoramica del flusso**  
  
Il workflow completo è questo:  
1. acquisizione input e setup cartelle  
2. download video da YouTube  
3. download sottotitoli automatici e creazione dell'SRT sorgente  
4. estrazione screenshot slide + creazione slides.csv  
5. esportazione del testo per slide in chunk per LLM  
6. correzione dei chunk con ChatGPT  
7. ricomposizione dei chunk corretti in un file unico strutturato  
8. generazione di PDF e DOCX finali  
9. pulizia dei file intermedi e spostamento degli output finali  
  
⸻  
  
**Step 1 --- Input iniziali**  
  
Lo script orchestrator Slidescribe_v2.sh chiede all'utente:  
• il path della cartella di lavoro (WORKDIR)  
• l'URL YouTube del video  
• il nome base del progetto / file (VIDEO_BASENAME)  
• il prompt da usare per ChatGPT  
• la modalità ROI per lo screenshot grabber, se necessaria  
  
Da questi valori costruisce tutti i path principali del progetto.  
  
Esempio concettuale:  
• video finale: \${WORKDIR}/\${VIDEO_BASENAME}.mkv  
• SRT sorgente: \${WORKDIR}/\${VIDEO_BASENAME}.original.srt  
• cartella slide: \${WORKDIR}/\${VIDEO_BASENAME} slides  
• chunk LLM: \${WORKDIR}/llm_chunks  
• chunk corretti: \${WORKDIR}/llm_corrected  
• merge finale testi: \${WORKDIR}/llm_merged  
• log: \${WORKDIR}/logs  
  
⸻  
  
**Step 2 --- Download del video**  
  
Lo script scarica il video da YouTube con yt-dlp.  
  
Scopo:  
• ottenere il file video locale da cui estrarre le slide  
• avere un file stabile e riproducibile per tutta la pipeline  
  
Output:  
• \${WORKDIR}/\${VIDEO_BASENAME}.mkv  
  
Se il file esiste già, questo step viene saltato.  
  
⸻  
  
**Step 3 --- Download dei sottotitoli automatici**  
  
Lo script usa yt-dlp per scaricare i sottotitoli automatici italiani e convertirli in formato SRT.  
  
Da questi file intermedi viene poi creato il file sorgente canonico:  
• \${WORKDIR}/\${VIDEO_BASENAME}.original.srt  
  
Questo file è l'unico SRT che poi viene usato davvero a valle da export_for_llm.py.  
  
I file .it.srt e .it-orig.srt sono intermedi e possono essere cancellati a fine pipeline.  
  
⸻  
  
**Step 4 --- Estrazione slide e creazione slides.csv**  
  
Lo script Screenshot_grabber.py prende il video e:  
• individua i cambi slide  
• salva gli screenshot nella cartella slide  
• genera un file slides.csv  
  
La cartella risultante è:  
• \${WORKDIR}/\${VIDEO_BASENAME} slides  
  
Tipicamente contiene:  
• slide_001\_\....png  
• slide_002\_\....png  
• ...  
• slides.csv  
  
**Ruolo di slides.csv**  
  
slides.csv è il file strutturale centrale per l'allineamento.  
  
Contiene per ogni slide almeno:  
• slide_index  
• timestamp_sec  
• timestamp_hms  
• filename  
  
Serve per sapere:  
• quante slide ci sono  
• in che momento compare ogni slide  
• quale immagine corrisponde a ogni slide  
  
⸻  
  
**Step 5 --- Esportazione per LLM (export_for_llm.py)**  
  
Questo è il primo passaggio chiave della nuova architettura.  
  
**Input**  
  
export_for_llm.py legge:  
• \${WORKDIR}/\${VIDEO_BASENAME}.original.srt  
• \${WORKDIR}/\${VIDEO_BASENAME} slides/slides.csv  
  
**Cosa fa**  
1. legge e parse l'SRT  
2. legge slides.csv  
3. assegna ogni blocco SRT alla slide corretta usando il **midpoint temporale** del blocco  
4. concatena il testo associato a ciascuna slide  
5. divide il risultato in chunk da 20 slide  
6. scrive file TXT nel formato intermedio per LLM  
  
**Perché questo step è importante**  
  
Qui avviene il cambio di paradigma:  
• il modello non lavora più su un SRT fragile  
• lavora su un file testuale strutturato **per slide**  
  
Questo riduce enormemente il rischio che un LLM:  
• cambi timestamp  
• unisca blocchi  
• rompa il layout  
• alteri la struttura del documento  
  
**Output**  
  
Cartella:  
• \${WORKDIR}/llm_chunks  
  
File tipici:  
• \${VIDEO_BASENAME}.chunk_001_slides_0001_0020.txt  
• \${VIDEO_BASENAME}.chunk_002_slides_0021_0040.txt  
• ...  
  
**Formato intermedio**  
  
Ogni chunk ha struttura rigida:  
  
`===== BEGIN CHUNK 001 =====`  
  
`----- BEGIN SLIDE 0001 -----`  
`TEXT:`  
`Testo associato alla slide 1.`  
  
`----- END SLIDE 0001 -----`  
  
`----- BEGIN SLIDE 0002 -----`  
`TEXT:`  
`Testo associato alla slide 2.`  
  
`----- END SLIDE 0002 -----`  
  
`===== END CHUNK 001 =====`  
  
Il modello può modificare solo il testo sotto TEXT:.  
  
⸻  
  
**Step 6 --- Correzione dei chunk con ChatGPT**  
  
Lo script orchestrator processa i chunk uno per uno.  
  
Per ogni chunk:  
1. fa l'upload del file TXT con chatgpt \--upload-file  
2. ottiene un file_id  
3. invia il prompt con il file allegato  
4. salva:  
• il file corretto in llm_corrected/  
• il raw JSON della risposta per debug  
  
**Input del modello**  
  
Il modello riceve:  
• un chunk di 20 slide  
• un prompt che specifica:  
• non alterare i delimitatori strutturali  
• non aggiungere contenuto nuovo  
• migliorare leggibilità, punteggiatura, termini tecnici  
• mantenere fedeltà semantica  
  
**Output**  
  
Cartella:  
• \${WORKDIR}/llm_corrected  
  
File tipici:  
• \${VIDEO_BASENAME}.chunk_001_slides_0001_0020.corrected.txt  
• \${VIDEO_BASENAME}.chunk_001_slides_0001_0020.raw.json  
  
**Vantaggio di questo approccio**  
  
Anziché dare in pasto 45.000 parole e 1100 blocchi a un LLM in una volta sola, si lavora su piccoli blocchi coerenti e facilmente validabili.  
  
Questo rende la pipeline:  
• più robusta  
• più controllabile  
• più economica da ritentare in caso di errore  
  
⸻  
  
**Step 7 --- Ricomposizione dei chunk corretti (import_corrected_for_pdf_docx.py)**  
  
Dopo che tutti i chunk sono stati corretti, entra in gioco il secondo script intermedio.  
  
**Input**  
  
import_corrected_for_pdf_docx.py legge:  
• tutti i .corrected.txt in \${WORKDIR}/llm_corrected  
  
**Cosa fa**  
1. parse ogni chunk corretto  
2. verifica che la struttura sia ancora valida  
3. controlla che non manchino slide  
4. controlla che non ci siano duplicati  
5. ricompone il testo finale slide per slide  
6. produce un file unico strutturato  
  
**Output**  
  
Cartella:  
• \${WORKDIR}/llm_merged  
  
File principali:  
• \${VIDEO_BASENAME}.slide_texts.json  
• \${VIDEO_BASENAME}.slide_texts.txt  
  
**Perché JSON**  
  
Il JSON è il formato macchina finale che userà il generatore PDF/DOCX.  
  
La struttura concettuale è:  
  
`{`  
`  ``"base_name"``: ``"OSAS2"``,`  
`  ``"total_slides"``: ``150``,`  
`  ``"slides"``: [`  
`    ``{`  
`      ``"slide_index"``: ``1``,`  
`      ``"text"``: ``"..."`  
`    ``}``,`  
`    ``{`  
`      ``"slide_index"``: ``2``,`  
`      ``"text"``: ``"..."`  
`    ``}`  
`  ]`  
`}`  
  
Il TXT parallelo è utile solo per debug umano.  
  
⸻  
  
**Step 8 --- Generazione PDF e DOCX (slides_and_texts_to_pdf.py)**  
  
A questo punto non serve più l'SRT.  
  
Lo script finale legge:  
• slides.csv  
• slide_texts.json  
• le immagini delle slide in SLIDES_DIR  
  
**Cosa fa**  
1. legge slides.csv  
2. legge slide_texts.json  
3. abbina ogni slide_index:  
• al suo file immagine  
• al suo testo corretto  
4. costruisce due output finali:  
• PDF  
• DOCX  
  
**Output iniziale**  
  
Vengono creati inizialmente in:  
• \${WORKDIR}/\${VIDEO_BASENAME} slides/\${VIDEO_BASENAME}.pdf  
• \${WORKDIR}/\${VIDEO_BASENAME} slides/\${VIDEO_BASENAME}.docx  
  
**Contenuto del PDF / DOCX**  
  
Per ogni slide:  
• intestazione slide  
• immagine della slide  
• testo associato e corretto  
  
L'output è pensato come materiale consultabile / dispensa.  
  
⸻  
  
**Step 9 --- Spostamento output finali**  
  
Come ultimo passaggio dell'orchestrator:  
• il PDF viene spostato da SLIDES_DIR a WORKDIR  
• il DOCX viene spostato da SLIDES_DIR a WORKDIR  
  
Output finali desiderati:  
• \${WORKDIR}/\${VIDEO_BASENAME}.pdf  
• \${WORKDIR}/\${VIDEO_BASENAME}.docx  
  
Questo tiene separati:  
• file finali  
• file strutturali  
• immagini slide  
• intermedi LLM  
  
⸻  
  
**Step 10 --- Pulizia file intermedi SRT**  
  
A fine pipeline vengono rimossi gli SRT intermedi non più necessari:  
• \${WORKDIR}/\${VIDEO_BASENAME}.it.srt  
• \${WORKDIR}/\${VIDEO_BASENAME}.it-orig.srt  
  
Viene mantenuto solo:  
• \${WORKDIR}/\${VIDEO_BASENAME}.original.srt  
  
Così rimane solo il file effettivamente usato da export_for_llm.py.  
  
⸻  
  
**Cartelle principali del progetto**  
  
A regime, la struttura logica è questa:  
  
`WORKDIR/`  
`├``─``─`` VIDEO_BASENAME.mkv`  
`├``─``─`` VIDEO_BASENAME.original.srt`  
`├``─``─`` VIDEO_BASENAME.pdf`  
`├``─``─`` VIDEO_BASENAME.docx`  
`├``─``─`` logs/`  
`├``─``─`` llm_chunks/`  
`├``─``─`` llm_corrected/`  
`├``─``─`` llm_merged/`  
`└``─``─`` VIDEO_BASENAME slides/`  
`    ``├``─``─`` slides.csv`  
`    ``├``─``─`` slide_001_....png`  
`    ``├``─``─`` slide_002_....png`  
`    ``└``─``─`` ...`  
  
  
⸻  
  
**Perché questa architettura è migliore della vecchia**  
  
La pipeline originale cercava di far correggere direttamente un grande SRT al modello.  
  
Questo causava problemi tipici:  
• numerazione blocchi alterata  
• timestamp modificati  
• blocchi uniti o spezzati  
• struttura SRT rotta  
  
La nuova pipeline invece:  
• usa l'SRT solo come input temporale grezzo  
• riallinea il testo alle slide prima del passaggio LLM  
• manda al modello solo testo per slide in formato controllato  
• ricompone tutto a valle con validazione strutturata  
  
In pratica:  
• meno fragilità  
• più controllo  
• più facilità di debug  
• output finale molto più adatto a diventare dispensa  
  
⸻  
  
**Riassunto finale in una riga**  
  
Il workflow di Slidescribe v2 è:  
  
**video YouTube → SRT originale + slide screenshots → testo aggregato per slide → correzione LLM a chunk → ricomposizione strutturata → PDF/DOCX finali**  
  
⸻  
  
**Possibili miglioramenti futuri**  
  
Upgrade sensati da considerare in futuro:  
  
**1. Cache per chunk già corretti**  
  
Se un chunk non cambia, si evita di reinviarlo a ChatGPT.  
  
**2. Validazione automatica ancora più severa**  
  
Per esempio controllo dei delimitatori e del numero di slide già subito dopo ogni risposta del modello.  
  
**3. Parallelizzazione dei chunk LLM**  
  
Con video lunghi può far risparmiare parecchio tempo.  
  
**4. Retry automatico dei chunk falliti**  
  
Se un chunk torna malformato, si può ritentare senza rifare tutta la pipeline.  
  
**5. Prompt versioning**  
  
Salvare il prompt usato per ogni run migliora tracciabilità e riproducibilità.

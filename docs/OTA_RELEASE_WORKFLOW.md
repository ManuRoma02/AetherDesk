# Flusso di Rilascio e Distribuzione Aggiornamenti Firmati (OTA)

Questo documento descrive il flusso completo e sicuro per creare, firmare crittograficamente e distribuire gli aggiornamenti di **AetherDesk** via GitHub Releases su tutti i tuoi PC Windows, consentendoti di aggiornare le macchine con un semplice clic di conferma e senza dover trasferire manualmente i file eseguibili.

---

## 1. Architettura di Sicurezza dell'OTA

Il sistema di aggiornamento adotta una politica di sicurezza **fail-closed**:
- **Trust Anchor Incorporato (Ed25519, RFC 8032)**: l'eseguibile installato sui PC client contiene cablata nel codice sorgente la sola **chiave pubblica** del fornitore ([`OTA_VENDOR_PUBLIC_KEY_HEX` in `src/ota.py`](file:///src/ota.py#L26)). Essa rappresenta la radice di attendibilità di tutti i controlli crittografici.
- **Chiave Privata Strettamente Offline**: la chiave privata Ed25519 usata per firmare i manifest risiede **esclusivamente** sul tuo PC sicuro di sviluppo (o su storage cifrato offline). Non deve **mai** essere inserita nel repository Git, tracciata nel codice o inclusa nei binari distribuiti.
- **Doppio Vincolo Crittografico (Binding Firma + Digest)**: la firma Ed25519 autentica la stringa canonica `AETHER_OTA:{versione}:{filename}:{sha256}:{dimensione}`. Il client convalida la firma prima del download e verifica l'hash SHA-256 esatto e l'header PE (`MZ`) prima dello staging.
- **Politica Anti-Rollback**: il client accetta solo versioni SemVer rigorosamente superiori a quella installata (`remote_parts > local_parts`), impedendo attacchi di downgrade a versioni vulnerabili.
- **Staging Dual-Slot (A/B) su Stesso Volume e Handshake di Readiness**: il binario scaricato viene prima copiato e convalidato (PE header, dimensione e digest SHA-256) in un file di staging sullo stesso volume dell'eseguibile di destinazione (`~{name}.new`). Lo script watchdog di supervisione viene creato e avviato in background (`subprocess.Popen`) **prima** di toccare o rinominare l'eseguibile attivo. Il processo chiamante attende un handshake esplicito di readiness tramite file marcatore univoco (`.ota_ready_{pid}`) scritto dal supervisore solo dopo l'avvenuta inizializzazione del proprio ambiente. Se l'handshake fallisce entro il timeout (3.0s) o si verifica un'eccezione imprevista, la funzione helper centralizzata `ensure_watchdog_terminated()` tenta la chiusura (`terminate()`) con attesa limitata e fallback a `kill()`, verificando tramite `poll()` che il processo sia effettivamente terminato. Solo se la terminazione è positivamente confermata, gli artefatti del tentativo (`~{name}.new`, batch e marcatori) vengono rimossi. Se la terminazione non può essere confermata, gli artefatti rimangono rigorosamente sul disco per non corrompere l'ambiente del supervisore potenzialmente attivo, e l'errore viene registrato senza dichiarare l'aggiornamento annullato in sicurezza (*fail-closed*).
- **Watchdog Robusto con Isolamento PID, Timeout Chiamante e Protezione Errori Swap**: lo script watchdog attende la terminazione del processo chiamante con un timeout limitato (30s) per non rimanere in esecuzione indefinita; se il chiamante non esce, annulla lo swap in sicurezza senza toccare l'eseguibile attivo. All'atto dello swap, l'assenza del file di staging (`~{name}.new`) viene gestita come errore esplicito (`:staging_missing`): registra la diagnostica, riavvia `current_exe` ed esce con `exit /b 1` senza mai raggiungere `update_ok`. Se la ridenominazione del vecchio eseguibile fallisce (`:swap_failed_backup`), il watchdog non cancella né `current_exe` né il backup: verifica quale binario è integro sul disco, lo riavvia per garantire la continuità del servizio e termina con `exit /b 1`.
- **Monitoraggio Boot, Conferma Positiva Esplicita e Rollback Failsafe**: il watchdog avvia la nuova versione catturando il suo **PID esatto** tramite PowerShell (`Start-Process -PassThru`), monitorando esclusivamente quel PID per 25 secondi. La conferma del boot è strettamente positiva: `confirm_boot()` crea il marcatore univoco `.ota_confirmed_{PID}` lasciandolo integro su disco per il supervisore. Il watchdog consuma il marcatore e solo a quel punto elimina `.exe.old` e salta a `:update_ok`. Se il backup scompare senza che sia stato emesso il marcatore di conferma valido, il watchdog rifiuta di considerare riuscito l'aggiornamento, registra l'errore critico in `.ota_rollback_failure.log` ed esce con codice di errore `exit /b 1` senza mai raggiungere `:update_ok`. Se la nuova versione crasha o non conferma il boot entro 25 secondi, il watchdog termina esclusivamente quel PID (`taskkill /f /pid %NEW_PID%`), rimuove il binario fallito e ripristina `.exe.old` verificando rigorosamente l'esito del ripristino (`errorlevel` ed esistenza del file). Se il ripristino dovesse fallire, il watchdog **non elimina mai il file di backup**, genera un file di diagnostica dettagliato (`.ota_rollback_failure.log`) ed esce con codice di errore `exit /b 1`.
- **Protezione Stato TESTING e Limiti Realistici di Boot Recovery**: se l'applicazione è già in stato `TESTING` (aggiornamento precedente avviato ma non ancora confermato), qualsiasi richiesta di aggiornamento concorrente viene rifiutata *fail-closed* per non distruggere il backup attivo. La funzione `cleanup_old_versions()` preserva rigorosamente il marcatore `.ota_confirmed_{PID}` del processo corrente anche se invocata con `force=True`, garantendo che rimanga disponibile sul disco fino alla sua regolare consumazione da parte del watchdog. La funzione `check_boot_state()` fornisce una procedura di auto-ripristino qualora venga invocata da installer o script amministrativi in assenza dell'eseguibile primario; tuttavia, in caso di blackout o riavvio improvviso del PC nella finestra in cui l'eseguibile primario è assente, Windows non può avviare un file mancante (`ERROR_FILE_NOT_FOUND`), rendendo necessario il ripristino manuale o tramite un launcher/servizio esterno.
- **Ownership Rigorosa dei File Temporanei**: la funzione `_safe_cleanup_temp()` applica un controllo a doppio vincolo: valida **sia** la stretta appartenenza della directory del file temporaneo alla cartella di staging dedicata (`get_ota_staging_dir()`) **sia** il formato del nome (`aether_ota_*.exe`). Qualsiasi file al di fuori della cartella di staging non viene mai eliminato, anche se presenta il medesimo prefisso, scongiurando qualsiasi rischio di cancellazione di file utente o di sistema.
- **P2P OTA Disabilitato**: la distribuzione diretta P2P tra host e client è disabilitata fail-closed finché non implementerà la medesima verifica di firma fornitore.
- **Isolamento Servizio Windows (Nessun OTA)**: l'aggiornamento OTA è supportato esclusivamente per l'applicazione interattiva in sessione utente (`ClientViewer` / `HostTray`). Il servizio di sistema Windows in Session 0 (`src/host_service.py`) non dispone di desktop interattivo né di SendInput e **non supporta gli aggiornamenti OTA** nel codice attuale.

---

## 2. Il Principio del Trust Anchor e la Necessità del Bootstrap Iniziale

> [!IMPORTANT]
> **Come funziona il Trust Anchor e perché il cambio di chiave richiede un Bootstrap Manuale**:
>
> La chiave pubblica incorporata nell'applicazione è il riferimento di fiducia assoluto. Un client già installato accetta **esclusivamente** manifest firmati con la chiave privata corrispondente alla chiave pubblica presente nel proprio binario.
>
> - **Impossibilità di migrazione OTA con chiave sconosciuta**: se i tuoi PC Windows hanno attualmente installata una versione compilata con una chiave pubblica diversa (o con la chiave predefinita segnaposto), **non possono** ricevere via OTA un aggiornamento firmato con una nuova chiave privata personale. Il client rifiuterà categoricamente il manifest (*fail-closed*).
> - **Fase di Bootstrap una tantum**: quando imposti per la prima volta la tua chiave personale, devi effettuare **una sola installazione manuale iniziale** del binario compilato con la tua nuova chiave pubblica su tutti i tuoi PC Windows tramite copia manuale di `AetherDesk.exe` nella sessione desktop dell'utente. *Nota*: `scripts/install_service.bat` blocca intenzionalmente l'installazione come servizio Windows SYSTEM in Session 0 (Session 0 non dispone di interfaccia desktop né di instradamento input); il servizio Windows non partecipa al flusso OTA.
> - **Rilasci successivi via OTA**: da quel momento in poi, tutti i successivi aggiornamenti (v1.4.1, v1.5.0, v2.0.0, ...) firmati con la tua chiave privata offline verranno riconosciuti, verificati e applicati via OTA senza mai più toccare i PC manualmente.

---

## 3. Prerequisiti Bloccanti prima della Build

> [!WARNING]
> **La DLL nativa `aether_core.dll` non è attualmente compilata.**
>
> Il file di specifica PyInstaller [`RemoteClient.spec`](file:///RemoteClient.spec#L18-L24) include un blocco di sicurezza vincolante:
> ```python
> if not native_dll:
>     raise SystemExit(
>         "\n[ERRORE PACKAGING BLOCCANTE] aether_core.dll non trovata.\n"
>         "La pipeline nativa H.264/WebRTC richiede il binario compilato prima del packaging PyInstaller.\n"
>         "Eseguire 'cargo build --release --manifest-path native/aether_core/Cargo.toml' o utilizzare 'scripts\\build_exe.bat'."
>     )
> ```
> Tentare di compilare l'eseguibile PyInstaller senza aver prima compilato il crate Rust fallisce immediatamente con `SystemExit`.

### Prerequisiti software sull'ambiente di sviluppo:
1. **Rust Toolchain**: `rustc` e `cargo` installati (da [rustup.rs](https://rustup.rs)).
2. **Python 3.10+** con le dipendenze di progetto installate (`cryptography`, `pyinstaller`, `websockets`, `pillow`, ecc.).

---

## 4. Fase A: Configurazione Iniziale e Bootstrap (Una Tantum)

Esegui questi passaggi una sola volta per predisporre il tuo ambiente:

### Passo 1: Genera la tua coppia di chiavi Ed25519 offline con cifratura PEM PKCS#8
Esegui questo snippet in un terminale Python locale sul tuo PC sicuro per generare una chiave protetta da passphrase:
```python
import getpass
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

# 1. Richiedi una passphrase robusta in modo interattivo e protetto (senza eco a video)
passphrase = getpass.getpass("Inserisci una passphrase per proteggere la chiave privata: ").encode("utf-8")
confirm = getpass.getpass("Conferma passphrase: ").encode("utf-8")
if passphrase != confirm or not passphrase:
    raise ValueError("Passphrase non valide o non corrispondenti!")

# 2. Genera la coppia di chiavi crittografica Ed25519 (RFC 8032)
private_key = ed25519.Ed25519PrivateKey.generate()

# 3. Esporta la chiave privata in formato standard PEM PKCS#8 cifrato con BestAvailableEncryption
pem_encrypted = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.BestAvailableEncryption(passphrase)
)

# 4. Salva il file PEM cifrato in una cartella sicura FUORI dal repository Git
key_path = Path(r"C:\Users\manue\Sicurezza\aether_ota_private.pem")
key_path.parent.mkdir(parents=True, exist_ok=True)
key_path.write_bytes(pem_encrypted)

# 5. Estrai e visualizza la chiave pubblica da fissare nell'applicazione
pub_hex = private_key.public_key().public_bytes_raw().hex()
print(f"\n[OK] Chiave privata cifrata salvata in: {key_path}")
print("CHIAVE PUBBLICA (TRUST ANCHOR) DA FISSARE IN src/ota.py:")
print(pub_hex)
```

> [!CAUTION]
> - **Niente chiavi in chiaro**: il salvataggio in formato grezzo (raw 32B non cifrato) o esadecimale in chiaro **non è consigliato** in quanto esporrebbe la chiave a furto in caso di accesso non autorizzato al disco. La procedura raccomandata utilizza **PEM PKCS#8 cifrato con passphrase**.
> - Quando eseguirai `scripts/sign_release.py`, lo script rileverà automaticamente la cifratura del file PEM e richiederà la passphrase tramite il prompt protetto `getpass.getpass()` senza mai stamparla a schermo o salvarla nei log.
> - Custodisci il file `aether_ota_private.pem` al sicuro e non condividerlo mai.
> - [`.gitignore`](file:///.gitignore) esclude già rigorosamente file `*.key`, `*.pem`, `*.priv` e `*private_key*`.

### Passo 2: Fissa la chiave pubblica in `src/ota.py`
Apri [`src/ota.py`](file:///src/ota.py#L26) e imposta la tua chiave pubblica esadecimale (64 caratteri):
```python
OTA_VENDOR_PUBLIC_KEY_HEX = "la_tua_chiave_pubblica_esadecimale_a_64_caratteri"
```

### Passo 3: Compila il primo binario di Bootstrap
1. Compila la DLL nativa:
   ```cmd
   cargo build --release --manifest-path native\aether_core\Cargo.toml
   ```
2. Compila l'eseguibile PyInstaller:
   ```cmd
   scripts\build_exe.bat
   ```
   L'eseguibile risultante è `dist\AetherDesk.exe`.

### Passo 4: Installa manualmente il binario di Bootstrap sui tuoi PC
Copia `dist\AetherDesk.exe` sui tuoi PC Windows fisici all'interno della normale sessione desktop utente.
> [!IMPORTANT]
> **Nessun supporto per installazione come servizio Windows**: non tentare di utilizzare `scripts\install_service.bat`: lo script blocca intenzionalmente l'installazione (`exit /b 1`) a causa dell'isolamento di Windows Session 0 (mancanza di desktop grafico e impossibilità di instradare `SendInput` verso le sessioni utente). Il servizio Windows non partecipa né supporta gli aggiornamenti OTA. Il percorso supportato per AetherDesk e il suo motore OTA è esclusivamente l'esecuzione interattiva in sessione utente (`AetherDesk.exe` o `AetherDesk.exe --host`). Questo passaggio manuale iniziale fissa la tua chiave pubblica di trust su tutte le macchine.

---

## 5. Fase B: Procedura per i Rilasci Successivi via OTA

Per ogni nuovo rilascio (es. `1.5.0`):

### Passo 1: Aggiorna la versione nel codice
In [`src/ota.py`](file:///src/ota.py#L20), imposta la nuova versione:
```python
CURRENT_VERSION = "1.5.0"
```

### Passo 2: Compila la nuova versione
Esegui la compilazione completa:
```cmd
cargo build --release --manifest-path native\aether_core\Cargo.toml
scripts\build_exe.bat
```
Verifica che l'eseguibile sia generato in `dist\AetherDesk.exe`.

### Passo 3: Firma fail-closed con `scripts/sign_release.py`
Esegui lo strumento di firma passando il percorso del file PEM cifrato della tua chiave privata locale:
```cmd
python scripts\sign_release.py --binary dist\AetherDesk.exe --version 1.5.0 --key-file C:\Users\manue\Sicurezza\aether_ota_private.pem
```
Lo script richiederà la passphrase del file PEM in modo interattivo e protetto:
```
Inserisci passphrase per la chiave privata PEM: [input nascosto]
```

**Garanzie Fail-Closed dello script**:
- Lo script verifica preventivamente che la chiave privata fornita derivi esattamente la chiave pubblica fissata in `OTA_VENDOR_PUBLIC_KEY_HEX`.
- **Nessuna opzione di override**: non esiste alcun argomento o flag CLI per sostituire o aggirare il trust anchor incorporato; il tool usa sempre e soltanto la chiave di trust compilata nell'applicazione.
- Se la chiave non corrisponde, **il processo si interrompe immediatamente con codice di errore**, senza creare o sovrascrivere `manifest.json`.
- La firma viene convalidata internamente usando la chiave di trust dell'applicazione prima di autorizzare la scrittura del manifest.
- Lo script salva `dist\manifest.json` e stampa il blocco `AETHER_MANIFEST:` per GitHub.

### Passo 4: Pubblica la Release su GitHub
1. Vai su GitHub (`ManuRoma02/AetherDesk`) > **Releases** > **Draft a new release**.
2. **Tag version**: `v1.5.0` (il prefisso `v` è supportato).
3. **Release title**: `AetherDesk v1.5.0`.
4. **Description**: incolla il blocco stampato da `sign_release.py`:
   ```markdown
   AETHER_MANIFEST:
   {
     "version": "1.5.0",
     "filename": "AetherDesk.exe",
     "sha256": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "size": 15420000,
     "signature": "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
   }
   ---
   ```
5. **Attach binaries**: allega come file:
   - `AetherDesk.exe`
   - `manifest.json`
6. Clicca **Publish release**.

---

## 6. Coerenza Asset, Nomi Eseguibile e Gestione Release Multi-Eseguibile

Per garantire massima robustezza quando una release contiene molteplici file:
- **Eseguibile Ufficiale Supportato**: il binario ufficiale prodotto dalle specifiche di packaging PyInstaller ([`RemoteClient.spec`](file:///RemoteClient.spec#L53)) è `AetherDesk.exe`.
- `sign_release.py` fissa nel manifest firmato il nome esatto dell'eseguibile locale (`"filename": "AetherDesk.exe"`).
- Quando il client interroga GitHub, scarica e verifica prima il manifest crittografico.
- Dal manifest autenticato, estrae il nome asset atteso e cerca negli asset della release GitHub il file con il medesimo nome.
- **Se la release contiene molteplici eseguibili** (es. `Setup.exe`, `AetherDesk.exe`, `RemoteClient.exe`), il client seleziona rigorosamente solo quello autenticato nel manifest (`AetherDesk.exe`).
- **Se l'asset autenticato è assente** o se sono presenti **molteplici asset ambigui con lo stesso nome**, il client rifiuta l'aggiornamento *fail-closed* senza procedere al download.
- **Comportamento in caso di eseguibile locale rinominato**: `apply_in_place_update()` sostituisce l'eseguibile del processo attivo (`current_exe`). Se l'utente ha rinominato manualmente l'eseguibile sul disco (es. `MyRemote.exe`), l'aggiornamento sostituisce il file preservando il nome locale corrente (`MyRemote.exe`), registrando un avviso informativo sulla differenza rispetto al nome canonico del manifest.

---

## 7. Esperienza Utente, Watchdog e Flusso di Rollback Automatico

Sui PC client su cui è installata la versione di bootstrap:

1. **Rilevamento in background**: il client controlla periodicamente l'API di GitHub Releases (`https://api.github.com/repos/ManuRoma02/AetherDesk/releases/latest`).
2. **Verifica preventiva**: scarica il manifest, controlla l'anti-rollback (`1.5.0 > 1.4.0`) e valida la firma Ed25519 con la chiave di trust. Se non valida, l'aggiornamento viene ignorato.
3. **Richiesta di conferma utente (non invisibile/silenzioso)**:
   L'applicazione mostra una finestra modale all'utente:
   ```
   Aggiornamento Disponibile
   È disponibile una nuova versione di AetherDesk!
   • Versione Installata: v1.4.0
   • Nuova Versione: v1.5.0
   • Dimensione: 15.4 MB

   Vuoi scaricare l'aggiornamento e riavviare adesso automaticamente? [Sì] [No]
   ```
4. **Download e Validazione Locale**:
   Se l'utente preme **Sì**:
   - Viene mostrata la progressione percentuale del download.
   - Il binario scaricato viene verificato: magic header `MZ`, dimensione >= 1MB e digest SHA-256 esatto rispetto al manifest. In caso di errore, il file temporaneo viene eliminato con pulizia immediata garantita.
5. **Staging su Stesso Volume e Handshake di Readiness Prima dello Swap**:
   - `apply_in_place_update()` controlla preventivamente che l'applicazione non sia in stato `TESTING` (rifiutando aggiornamenti concorrenti se un riavvio precedente non è ancora stato confermato).
   - Rimuove eventuali file obsoleti non bloccati tramite `cleanup_old_versions(force=False)` senza mai toccare backup attivi.
   - Copia il binario convalidato dal percorso temporaneo nella directory di destinazione sullo stesso volume: `current_exe.parent / f"~{current_exe.name}.new"`.
   - Riconvalida il file di staging (`validate_executable_file`: dimensione >= 1MB, header PE `MZ`, digest SHA-256 esatto rispetto al manifest firmato). Se la copia o la convalida fallisce, il file `~{name}.new` viene eliminato, la directory temporanea di staging viene pulita e l'operazione abortisce: `current_exe` rimane completamente intatto e non viene toccato.
   - Genera lo script batch detached di supervisione (`aether_restart_{pid}.bat`) e lo avvia in background con `subprocess.Popen` (`CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`, `close_fds=True`, variabili d'ambiente PyInstaller resettate).
   - **Handshake di Readiness Vincolato**: il processo chiamante attende la creazione del file marcatore `.ota_ready_{pid}` da parte del batch entro un timeout definito (`readiness_timeout`, default 3.0s). Il batch emette questo segnale solo dopo aver impostato il proprio ambiente e directory di lavoro.
   - **Gestione fail-closed su mancata readiness ed errori post-spawn**: se `subprocess.Popen` fallisce, se il marcatore di readiness non viene prodotto entro il timeout o se si verifica un'eccezione imprevista nel flusso chiamante, interviene `ensure_watchdog_terminated()`. Essa tenta `terminate()` e attende con timeout (2.0s); se l'attesa scade o fallisce, tenta `kill()` e attende nuovamente (1.0s); infine verifica con `poll()` l'effettiva cessazione del processo. Se la terminazione **non può essere confermata** (processo ancora vivo o stato sconosciuto), il sistema **non elimina** gli artefatti operativi del supervisore (`ready_marker`, `restart_bat` o `staged_target`), preserva `current_exe` e `old_backup`, registra un errore critico e restituisce un esito che segnala esplicitamente che il supervisore potrebbe essere ancora attivo (senza descrivere la situazione come completata o annullata in sicurezza), mentre pulisce in sicurezza il download temporaneo indipendente in staging (`_safe_cleanup_temp()`) per evitare accumuli su disco. Se invece la terminazione è **positivamente confermata**, vengono rimossi anche tutti gli artefatti del supervisore (`~{name}.new`, file `.bat`, marcatore di readiness), lasciando l'eseguibile attivo intatto.
   - Se il supervisore è confermato pronto, il processo corrente termina in modo ordinato (`os._exit(0)`).
6. **Esecuzione dello Swap su Stesso Volume, Monitoraggio PID e Rollback Supervisato**:
   - Lo script batch attende la terminazione ordinata del vecchio processo (`pid`) con un timeout esplicito di 30 secondi (`:wait_exit`). Se il chiamante non termina entro 30s (`:caller_timed_out`), il watchdog registra un avviso in `.ota_rollback_failure.log`, cancella il marcatore e `~{name}.new`, ed esce senza toccare l'eseguibile attivo.
   - All'atto dello swap, verifica rigorosamente la presenza del file di staging: `if not exist staged_target goto staging_missing`. Se assente (`:staging_missing`), registra un errore critico in `.ota_rollback_failure.log`, riavvia `current_exe` se presente sul disco ed esce con `exit /b 1` senza mai entrare in `:update_ok`.
   - Se la ridenominazione del vecchio eseguibile fallisce (`:swap_failed_backup`), il batch rimuove lo staging unswapped, non cancella né `current_exe` né `old_backup`, verifica quale eseguibile è integro e lo riavvia per mantenere il servizio attivo, registrando la diagnostica ed uscendo con `exit /b 1`.
   - Se la ridenominazione dello staging su `current_exe` fallisce (`:swap_failed_install`), il batch registra l'avviso ed entra in `:do_rollback`.
   - Se lo swap riesce, avvia la nuova versione catturando il suo **PID esatto (`NEW_PID`)** tramite PowerShell (`(Start-Process -FilePath ... -PassThru).Id`).
   - Se `NEW_PID` non viene acquisito o risulta vuoto, salta immediatamente a `:do_rollback`.
   - Il watchdog entra in un loop di monitoraggio attivo di 25 secondi vincolato esclusivamente al PID avviato (`tasklist /fi "PID eq %NEW_PID%"`).
   - **Scenario di Successo (Conferma Esplicita e Raggiungibilità di `:update_ok`)**: la nuova versione si avvia in stato `TESTING`. Una volta completata l'inizializzazione della GUI e delle connessioni, chiama `confirm_boot()`. `confirm_boot()` crea esclusivamente il marcatore di conferma per il PID corrente (`.ota_confirmed_{PID}`) e lo lascia integro sul disco per il supervisore. Il watchdog rileva la presenza del marcatore corrispondente a `%NEW_PID%`, lo elimina, rimuove `old_backup` e salta a `:update_ok` completando la pulizia e la chiusura con successo.
   - **Rifiuto su Backup Mancante senza Conferma Positiva**: se durante il monitoraggio `old_backup` dovesse risultare assente senza che sia stato prima rilevato il marcatore di conferma valido (`.ota_confirmed_%NEW_PID%`), il watchdog non assume il successo: salta a `:unconfirmed_missing_backup`, scrive l'errore critico in `.ota_rollback_failure.log` ed esce con `exit /b 1` senza mai raggiungere `:update_ok`.
   - **Scenario di Fallimento / Crash (Rollback con Diagnostica e Preservazione Backup)**: se la nuova versione crasha all'avvio (es. DLL mancante, eccezione all'import) o non chiama `confirm_boot()` entro 25 secondi, il watchdog:
     1. Termina esclusivamente quel processo (`taskkill /f /pid %NEW_PID%`) senza toccare altre istanze o processi.
     2. Elimina il binario difettoso `current_exe`.
     3. Rimuove `staged_target` se ancora presente.
     4. Controlla l'esistenza del backup `old_backup`. Se mancante (`:rollback_missing_backup`), scrive il log diagnostico ed esce (`exit /b 1`).
     5. Esegue il ripristino: `move /y old_backup current_exe`.
     6. Controlla l'esito: `if errorlevel 1 goto rollback_failed` e `if not exist current_exe goto rollback_failed`.
     7. **Se il ripristino fallisce** (`:rollback_failed`): **NON elimina mai il backup**, crea il file di diagnostica dettagliato `.ota_rollback_failure.log` con timestamp, PID del processo fallito, percorso del backup valido preservato e l'azione manuale richiesta, ed esce con `exit /b 1`.
     8. **Se il ripristino ha successo**: riavvia `current_exe` con gli argomenti originali e termina rimuovendo se stesso.

---

## 8. Limiti Realistici di Tolleranza ai Crash, Assenza di Launcher e Interruzioni di Alimentazione

L'architettura OTA di AetherDesk gestisce con precisione i comportamenti del filesystem Windows (NTFS) e i vincoli intrinseci dell'esecuzione di processi:
- **Lock sui File Eseguibili in Windows**: su Windows, un file `.exe` in esecuzione è bloccato dal kernel contro la sovrascrittura diretta (`ERROR_SHARING_VIOLATION`), ma può essere ridenominato se aperto con flag di condivisione compatibile. L'approccio implementato risolve questo vincolo eseguendo prima la copia e validazione in staging locale sullo stesso volume (`~{name}.new`), delegando poi al supervisore batch lo swap atomico di metadati NTFS (`move /y`) una volta che il processo attivo ha terminato l'esecuzione.
- **Resilienza a Crash Applicativi e Boot Falliti**:
  - Se la nuova versione compilata presenta difetti a runtime, crash improvvisi, eccezioni all'import o mancata risposta della GUI entro il timeout di 25 secondi, il supervisore watchdog attivo intercetta l'anomalia, termina esclusivamente il processo figlio fallito, ripristina la versione precedente `AetherDesk.exe.old` e la riavvia automaticamente.
- **Interruzioni di Alimentazione / OS Reboot e Limiti Realistici (Assenza di Launcher Esterno)**:
  - *Distinzione fondamentale tra crash di processo e spegnimento del sistema*: il watchdog batch garantisce il ripristino automatico fintanto che il sistema operativo Windows rimane attivo durante la fase di swap e monitoraggio.
  - *Comportamento all'avvio di Windows con eseguibile assente*: se dovesse verificarsi un'interruzione violenta di alimentazione hardware (blackout o spegnimento improvviso) o un riavvio forzato del sistema operativo nell'esatta frazione di secondo in cui `current_exe` è stato ridenominato in `.exe.old` ma `staged_target` non è ancora stato spostato al suo posto, `current_exe` risulterà fisicamente assente sul disco al riavvio del PC.
  - *Perché il recupero automatico all'avvio del PC non è possibile senza launcher o servizio esterno*: su Windows, quando un utente fa doppio clic o il sistema tenta di avviare un programma il cui file `.exe` non esiste sul disco, il kernel loader restituisce immediatamente `ERROR_FILE_NOT_FOUND` a livello di sistema operativo. Nessun codice interno all'applicazione o all'interprete Python può essere caricato o eseguito.
  - *Ruolo reale di `check_boot_state()`*: la funzione `check_boot_state()` include la logica per rilevare `current_exe` mancante e ripristinare `old_backup`; tuttavia, questa procedura può entrare in azione **esclusivamente se invocata da un ambiente esterno già in esecuzione** (es. un launcher separato, un installer di manutenzione, un task pianificato o uno script amministrativo). Non deve essere intesa come una capacità autonoma dell'eseguibile di ripristinarsi da solo quando l'eseguibile stesso non può essere avviato dal sistema operativo.
  - *Procedura di Ripristino Manuale in caso di interruzione hardware violenta durante lo swap*:
    1. Il file di backup `AetherDesk.exe.old` viene **sempre preservato integro** nella cartella dell'applicazione (non viene mai eliminato nei rami di errore).
    2. È sufficiente rinominare manualmente `AetherDesk.exe.old` in `AetherDesk.exe` per ripristinare istantaneamente il normale funzionamento. In caso di fallimento del ripristino da parte del watchdog, consultare il file `.ota_rollback_failure.log` generato nella cartella dell'eseguibile per dettagli e timestamp.

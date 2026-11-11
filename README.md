# project-nas-home

La Videoteca del Raspberry Pi: una pagina web per vedere, caricare e sistemare i film
che stanno sul Pi. Le info dei film (descrizione, durata, copertine) arrivano da TMDB.
Dalla scheda Rete controlla anche la rete di casa e ti avvisa su Telegram se trova qualcosa di strano.

Questo repository serve anche a portare i file sul Pi senza collegarsi dal Mac:
il Pi scarica da qui l'ultima versione e la installa.

## La prima volta, sul Pi

Dal terminale del Pi (tastiera e schermo collegati al Pi vanno benissimo):

```
sudo apt install -y git
git clone -b videoteca https://github.com/Stefold08/project-nas-home
bash project-nas-home/aggiorna.sh
```

Alla fine compare l'indirizzo da aprire nel browser, tipo `http://192.168.1.37:8080`.

Se il repository è privato, `git clone` chiede nome utente e un token di GitHub:
conviene lasciarlo pubblico, tanto qui dentro non ci sono segreti.

## Aggiornare

```
bash project-nas-home/aggiorna.sh
```

Oppure, una volta sola, accendi l'aggiornamento automatico: da quel momento il Pi
controlla GitHub ogni 15 minuti e installa da solo le versioni nuove.

```
bash project-nas-home/aggiorna.sh --automatico
```

Per spegnerlo: `--manuale`. Per vedere che versione c'è e se l'automatico è acceso: `--stato`.

## Controllo della rete

Il Pi può fare da DNS per tutta la casa: così vede quali siti contatta ogni dispositivo,
li confronta con un elenco di siti pericolosi (HaGeZi Threat Intelligence Feeds, aggiornato
da solo ogni giorno) e ti scrive su Telegram quando trova:

- un sito o un server pericoloso;
- tanti nomi a caso che non esistono, come fanno alcuni malware per trovare il loro server;
- tante richieste con nomi lunghissimi verso lo stesso dominio (un possibile «tunnel DNS»);
- un dispositivo nuovo in rete;
- il DNS del Pi che non risponde.

Non vede il contenuto di quello che fate: solo i nomi dei siti. La Videoteca non salva
l'elenco dei siti visitati: tiene solo quante richieste fa ogni dispositivo e gli avvisi.
I nomi passano per il registro di sistema del Pi, che si svuota da solo. Per accenderlo, una volta:

```
bash project-nas-home/rete.sh
```

Lo script installa dnsmasq e alla fine ti dice cosa cambiare sull'Iliadbox: un indirizzo
fisso per il Pi e il Pi come server DNS, anche per IPv6. Poi nella Videoteca apri la scheda
Rete e collega Telegram: ti serve un bot, che si crea in un minuto con @BotFather.

Con il controllo acceso, se il Pi si spegne in casa non si naviga: in caso di emergenza
togli il Pi dai server DNS dell'Iliadbox. Per spegnere tutto:
`bash project-nas-home/rete.sh --spegni`.

## Se qualcosa non va

```
bash project-nas-home/diagnosi.sh
```

Controlla la Videoteca, la rete e Tailscale senza cambiare niente, e stampa un riepilogo
da mandare a Claude.

## Usarla fuori casa

```
bash project-nas-home/tailscale.sh
```

Installa Tailscale sul Pi e lo collega al tuo account (inquadri un codice QR col telefono).
Poi installa l'app Tailscale sul Mac e sul telefono con lo stesso account: lo script ti
dice l'indirizzo da usare, tipo `http://100.x.y.z:8080`, valido da qualsiasi rete.

Ultimo passo, dal browser (non nel terminale del Pi): apri la pagina
[Machines](https://login.tailscale.com/admin/machines) della console di Tailscale, sulla riga
del Pi premi i tre puntini a destra e scegli «Disable key expiry». Senza questo, tra sei mesi
il Pi chiederebbe di rifare l'accesso e da fuori casa non lo raggiungeresti più.

## Cosa c'è qui dentro

| File | A cosa serve |
| --- | --- |
| `installa-videoteca.sh` | L'installatore, tutto in un file. Si può anche copiare a mano sul Pi e lanciare con `bash`. |
| `aggiorna.sh` | Scarica l'ultima versione da GitHub e la installa. |
| `tailscale.sh` | Installa Tailscale per l'accesso da fuori casa. |
| `rete.sh` | Accende (o spegne) il controllo della rete: dnsmasq come DNS di casa. |
| `diagnosi.sh` | Riepilogo dello stato del Pi da mandare a Claude quando qualcosa non va. |
| `videoteca/videoteca.py` | Il server (Python, nessuna libreria da installare). |
| `videoteca/index.html` | La pagina, con le schede Film e Rete. |
| `videoteca/rete.py` | Il controllo della rete: legge le richieste DNS, riconosce i dispositivi, manda gli avvisi. |
| `videoteca/installer-template.sh`, `videoteca/build.py` | Generano `installa-videoteca.sh`: dopo una modifica lancia `python3 videoteca/build.py`. |
| `test/` | Le prove automatiche usate per controllare le modifiche prima di pubblicarle. |

## Dove stanno le cose sul Pi

- Programma: `/opt/videoteca` (servizio `videoteca`, si avvia da solo all'accensione).
- Film: `/mnt/film`, una cartella per film.
- Chiave TMDB, password e token di Telegram: solo in `/opt/videoteca/config.json`, sul Pi. Non finiscono mai in questo repository.
- Controllo della rete: dispositivi e avvisi in `/opt/videoteca/rete-stato.json`, elenchi dei siti pericolosi in `/opt/videoteca/liste/`, configurazione di dnsmasq in `/etc/dnsmasq.d/videoteca.conf`.

Chi può modificare questo repository può far girare programmi sul Pi, soprattutto con
l'aggiornamento automatico acceso: dai l'accesso solo a chi ti fidi.

Le info sui film vengono da [TMDB](https://www.themoviedb.org); questo progetto non è approvato né certificato da TMDB.

# Legacy EMS Bot

Bot Discord pentru serverul de medici / EMS. Este pregătit pentru Railway și este făcut să ruleze pe două servere:

1. Serverul principal FiveM
2. Serverul Discord EMS / Medici

## Funcții

- Sistem demisie cu model obligatoriu:
  ```text
  Nume:
  Ore:
  Motiv:
  ```
- Butoane pentru staff:
  - Acceptă Demisia
  - Refuză Demisia
- Dacă demisia este refuzată, staff-ul trebuie să introducă motivul.
- Dacă demisia este acceptată, botul trimite log pe serverul EMS și pe serverul principal.
- Data și ora intrării sunt obligatorii. Dacă nu sunt setate, membrul nu poate depune demisia.
- Calcul precis: zile, ore și minute în departament.
- Rolurile se elimină manual de către conducere.

## Comenzi staff

### Setare data + ora intrării

```text
/setintrare @membru DD/MM/YYYY HH:MM
```

Exemplu:

```text
/setintrare @Jmarok 22/06/2026 20:30
```

### Verificare intrare

```text
/intrare @membru
```

### Ultimele demisii

```text
/demisii
```

## Model pentru membri

În canalul de demisii, membrul trebuie să scrie:

```text
Nume: Numele lui
Ore: 120
Motiv: Motivul complet al demisiei
```

Exemplu:

```text
Nume: Jmarok
Ore: 120
Motiv: Nu mai am timp să activez în departament.
```

## Railway Variables obligatorii

Adaugă aceste variabile în Railway:

```env
DISCORD_TOKEN=TOKEN_BOT
MAIN_GUILD_ID=1505903653079351357
EMS_GUILD_ID=ID_SERVER_MEDICI
DEMISIE_CHANNEL_ID=ID_CANALE_DEMISIE
EMS_LOG_CHANNEL_ID=ID_CANALE_LOG_EMS
MAIN_LOG_CHANNEL_ID=ID_CANALE_LOG_MAIN
STAFF_ROLE_IDS=ID_RUOLO_1,ID_RUOLO_2,ID_RUOLO_3
DB_PATH=/data/legacy_ems.db
TIMEZONE=Europe/Bucharest
DELETE_TRIGGER_MESSAGE=false
```

## Intents obligatorii

În Discord Developer Portal activează:

- MESSAGE CONTENT INTENT
- SERVER MEMBERS INTENT

## Storage Railway

Pentru ca datele din `/setintrare` să nu se piardă la redeploy/restart, creează un Railway Volume montat pe:

```text
/data
```

## Versiune

```text
1.0.0-legacy-ems
```

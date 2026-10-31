# TalkTogether 0.6.0 Beta – priprava za internet in posodobitve

Ta paket je **pripravljalni paket**. Strežnik še ni javno objavljen in posodabljalnik še nima nastavljenega GitHub repozitorija. Javni naslov dobimo šele po uspešni objavi storitve.

## Vsebina

- `client/` – aplikacija TalkTogether 0.6.0 Beta in Windows zaganjalnik.
- `server/` – FastAPI strežnik s Supabase preverjanjem, LiveKit žetoni in WebSocket signalizacijo.
- `render.yaml` – predloga za objavo strežnika na Renderju.

## A. Objava strežnika na Renderju

1. Ustvari GitHub repozitorij za projekt in vanj naloži vsebino tega paketa. V repozitorij **ne dodajaj** strežniške datoteke `.env`, API skrivnosti, gesel ali LiveKit API Secret.
2. V Renderju izberi ustvarjanje nove storitve iz Blueprinta in poveži repozitorij, v katerem je `render.yaml`.
3. Render bo prebral `render.yaml` in pripravil spletno storitev `talktogether-server`.
4. V nastavitvah storitve dodaj vrednosti za `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY` in `LIVEKIT_API_SECRET`. Vrednosti vnesi neposredno v zaščitena okoljska polja ponudnika. Ne pošiljaj jih v klepet in jih ne zapisuj v GitHub.
5. Po uspešni objavi odpri naslov storitve z dodatkom `/health`. Pričakovani odgovor je JSON s `status: ok` in različico strežnika.
6. Kopiraj osnovni HTTPS naslov storitve (brez `/health`) in ga vpiši v `client/client_config.json` v polje `server_url`. Primer oblike: `https://ime-storitve.onrender.com`. To je samo primer oblike, ne dejanski naslov tvoje storitve.

Render brezplačne spletne storitve lahko po 15 minutah brez dohodnega prometa preidejo v mirovanje. Ob naslednji povezavi se lahko prebujajo približno minuto. Aktivni odjemalec pošilja 25-sekundni signal aktivnosti; če ni nobenega uporabnika, se storitev lahko uspava. Za zanesljivo stalno dosegljivost je pozneje potreben vedno aktiven plačljiv načrt. Med mirovanjem ali ponovnim zagonom se trenutne WebSocket seje in aktivni klici prekinejo.

## B. Namestitev odjemalca na Windows

1. V `client/` kopiraj `.env.example` v novo datoteko `.env`.
2. V `.env` vpiši `SUPABASE_URL` in `SUPABASE_PUBLISHABLE_KEY` iz istega Supabase projekta, ki ga uporablja tvoj TalkTogether. To sta javni odjemalski nastavitvi. **Ne kopiraj sem `LIVEKIT_API_SECRET`.**
3. V `client_config.json` nastavi javni HTTPS naslov strežnika, ki ga dobiš po objavi na Renderju.
4. Namesti Python 3 in zaženi `install.bat` (ta namesti knjižnice iz `requirements.txt`).
5. Zaženi `run.bat`.
6. Za zvoke kopiraj svoja dva WAV-posnetka iz delujoče različice 0.5.0 v `client/sounds/calls/`, pri čemer ohrani obstoječi imeni.

## C. Nastavitev posodabljalnika

Posodabljalnik uporablja javne GitHub Releases. Pred namestitvijo preveri SHA-256 zgoščeno vrednost datoteke, ki jo vrne GitHub API.

1. Ustvari GitHub repozitorij za TalkTogether.
2. V `client/updater_config.json` zamenjaj `OWNER/REPOSITORY` z dejanskim `uporabnik/ime-repozitorija`.
3. Za vsako beta izdajo ustvari GitHub Release z oznako, na primer `v0.6.0-beta.2`, označi `prerelease = true` in naloži ZIP z imenom `TalkTogether-Client.zip`.
4. ZIP mora imeti `main.py`, `updater.py`, `run.bat`, `version.txt`, `client_config.json`, `updater_config.json`, `requirements.txt` in mapo `sounds` neposredno v korenu ZIP-a. V ZIP ne dodajaj `.env`.
5. Ob naslednjem zagonu prek `run.bat` bo posodabljalnik preveril GitHub, vprašal za dovoljenje, prenesel paket, preveril SHA-256, ga namestil in ponovno zagnal aplikacijo.

Prvi javni beta ZIP pripravi iz vsebine mape `client/` (brez `.env`). Ne nalagaj celotnega projekta kot odjemalskega ZIP-a.

## Varnost in omejitve beta različice

- `LIVEKIT_API_SECRET` ostane samo v zaščitenih strežniških okoljskih spremenljivkah.
- `.env` se ne sme nalagati v GitHub ali pošiljati Dušku.
- Strežnik hrani prisotnost in klice v pomnilniku enega procesa; konfiguracija Render uporablja en Uvicorn worker. Ob ponovnem zagonu se aktivni klici prekinejo.
- Prijatelj mora uporabljati isti Supabase projekt in mora biti prijavljen, da prejme WebSocket dohodni klic.
- Render Free je namenjen testiranju in ima omejitve mirovanja. Ta paket sam po sebi še ne pomeni, da je strežnik že javno objavljen.

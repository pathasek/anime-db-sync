# anime-db-sync

Noční stahování databáze anime pro osobní, nekomerční web.

- **Zdroje:** MyAnimeList přes [Tenrai](https://tenrai.org) a [AniList](https://anilist.co). Děkujeme oběma.
- **Tempo:** pod limity obou služeb. Tenrai nejvýš 108 dotazů za minutu a 36 000 za den, AniList 20 za minutu. Po opakovaném 429 se stahování na noc zastaví.
- **Data tady nejsou.** Repo obsahuje jen skript a workflow. Stažená data jdou do soukromého úložiště webu a web z nich používá jen zpracované soubory.

Webové soubory (`python build_web.py --data <složka>`, výstup do `web/`, formát verze 2):

- **Hned po přepnutí:** `meta.json`, `core.json`, `dict.json`, `al_core.json`. `meta.velikosti` uvádí u každého souboru `[bajty, gzip]` (bez `meta.json` samotného).
- **Na vyžádání:** `titles.json`, `syn/`, `recs.json`, `staff.json`, `cast.json`, `chars/`, `stats.json`, `rel.json`, `al_detail/`, `al_lide/`. Velikosti bloků jsou v `meta.bloky`.
- **`--al-tagy cele|okoli`:** výchozí `cele` (AniList u všech anime). `okoli` omezí `al_core`, `al_detail`, `al_lide`, hrany `recs.al`, aliasy AniListu v `titles` a zálohu synopse na malé okolí.
- Výstup je deterministický (stejná data, stejné bajty) a žádný soubor nepřesáhne 50 MB.

Novinky recenzí a posudků (`--texty`, v nočním běhu zapnuté, jen proces Tenrai):

- **Co čte:** Tenrai `/reviews/anime` (50 na stránku) a `/recommendations/anime` (100 na stránku) od nejnovějších. Čte, dokud celá stránka není starší než poslední viděné datum bez jednoho dne. Stejný limit a tempo jako zbytek běhu, před ním.
- **Kam píše:** `texty/recenze-RRRR-MM-DD.jsonl.gz` a `texty/posudky-RRRR-MM-DD.jsonl.gz` v datech (ne do repa), jeden záznam na řádek ve formátu základu. Soubor nese den textu, duplicity odstraní id (u posudků dvojice s otiskem textu).
- **Fronta:** `texty/fronta.json` říká, kterým anime přibyl text a od kdy. Stav čtení je ve `state.json` pod `texty`, souhrn v `meta.uplnost.texty`.
- **Nečekaná odpověď:** čtečka daného výpisu přestane, nic nezapíše a do logu dá jen počty. Běh pak skončí s kódem 1, ale stahování pokračuje.
- **`--texty-cely`:** čtvrtletní celý průchod (úpravy a smazané), v Actions vstup `texty_cely`. Zapíše `prehled-*.jsonl.gz` (id, den, otisk) a při dalším průchodu změněné a smazané.
- **Úklid (60 dní):** na konci úspěšného čtení noční běh smaže `texty/recenze-*`, `texty/posudky-*`, `smazane-*` a `prehled-*` starší než 60 dní podle data v názvu. Nejnovější přehled každého výpisu zůstává, je základem dalšího celého průchodu. Z fronty odejdou záznamy bez novinky za 60 dní (pole `posledni`, u starších záznamů `od`). Po anomálii se neuklízí. Cizí názvy se nemažou, v logu je jen počet.
- **Kdo zapisuje:** větev `animedb` přepisuje jen noční běh, PC ji nikdy nezapisuje. PC soubory jen čte a sloučí idempotentně podle `id` (posudky podle dvojice a otisku textu). Sloučit je potřeba do 60 dní.
- **Pojistka workflow:** `--texty-smazano SOUBOR` zapíše cesty plánovaně mazaných souborů dřív, než se smažou, a pojistka „zmizelo N souborů“ je odečte. Poplach tak spadne jen při nečekaném zmizení.
- **`--texty-od RRRR-MM-DD`:** počáteční datum, dokud čtečka nemá vlastní poslední viděné (výchozí 3. 11. 2026).

Testy (bez sítě):

```
python -m unittest discover -s tests -t .
```

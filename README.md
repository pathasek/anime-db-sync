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

Testy (bez sítě):

```
python -m unittest discover -s tests -t .
```

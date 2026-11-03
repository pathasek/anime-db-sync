# anime-db-sync

Noční stahování databáze anime pro osobní, nekomerční web.

- **Zdroje:** MyAnimeList přes [Tenrai](https://tenrai.org) a [AniList](https://anilist.co). Děkujeme oběma.
- **Tempo:** pod limity obou služeb. Tenrai nejvýš 108 dotazů za minutu a 36 000 za den, AniList 20 za minutu. Po opakovaném 429 se stahování na noc zastaví.
- **Data tady nejsou.** Repo obsahuje jen skript a workflow. Stažená data jdou do soukromého úložiště webu a web z nich používá jen zpracované soubory.
- **Výjimka do 11. 11. 2026:** data AniListu jsou se svolením týmu AniList i ve větvi `anilist`. Potom ji workflow samo smaže.

Testy (bez sítě):

```
python -m unittest discover -s tests -t .
```

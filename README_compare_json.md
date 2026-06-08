# compare_json.py

Скрипт `compare_json.py` собирает структуру `Example.json` — сравнительную «матрицу» фич Tantor DB по нескольким мажорным версиям. В отличие от `compare.py`, результат — не плоская таблица в Markdown/HTML, а JSON-документ, сгруппированный по разделам (например, *«Улучшения и оптимизация ядра»*, *«Дополнительная функциональность»*, *«Управление, мониторинг, профилирование»*, *«Надёжность и высокая доступность»* и т.д.). Этот JSON удобно отдавать дальше во фронт/рендерер сравнения изданий.

На вход:

- `config.toml` — какие версии, издания и пути к `conf.json`/`contrib.json` использовать;
- `descriptions.json` — каталог фич (группы → фичи) с человекочитаемыми именами, описаниями, `doc_url` и техническими идентификаторами `tech`.

На выходе:

- `Example.json` — `{versions, editions, groups[*].features[*].matrix}`. По каждому `tech` скрипт обходит все пары `(версия, издание)` из `config.toml` и расставляет признаки наличия фичи в этой ячейке.

## Чем отличается от `compare.py`

| | `compare.py` | `compare_json.py` |
|---|---|---|
| Источник описаний | `descriptions.toml` (опционально) | `descriptions.json` (обязательно) |
| Результат | `comparison.md` + `comparison.html` | `Example.json` |
| Структура вывода | 3 секции: `patches` / `utils` / `contrib` | группы фич из `descriptions.json` (без разделения по типу tech) |
| Колонки изданий | короткие имена (`se1c`, `certified_2`) | человекочитаемые (`Special Edition 1C`, `Certified исп. 2`) |
| Маппинг алиасов tech | нет | `[tech_aliases]` в `config.toml` |
| Игнор «служебных» tech | нет | `[ignored_tech]` в `config.toml` |
| Поле `pg-*` (есть в апстриме) | нет | переносится из `descriptions.json` 1‑в‑1 |

## Как устроен `Example.json`

```json
{
  "versions": ["18", "17", "16", "15", "14"],
  "editions": {
    "18": ["Basic Edition", "Special Edition", "Special Edition 1C"],
    "17": ["Basic Edition", "Special Edition", "Special Edition 1C", "Certified исп. 1", "Certified исп. 2"],
    "16": ["..."],
    "15": ["..."],
    "14": ["..."]
  },
  "groups": [
    {
      "name": "Улучшения и оптимизация ядра",
      "features": [
        {
          "name": "64-битный счетчик транзакций (XID)",
          "tech": "func/xid64",
          "description": "...",
          "matrix": {
            "18-Special Edition": true,
            "18-Special Edition 1C": true,
            "17-Special Edition": true,
            "...": true,
            "pg-18": true
          },
          "doc_url": "https://..."
        }
      ]
    }
  ]
}
```

### Как формируется `matrix`

Для каждой фичи `matrix` собирается **из двух слоёв**:

1. **Computed** — ключи вида `"<версия>-<длинное_имя_издания>"`. Пересчитываются с нуля каждый прогон: для каждой пары `(версия, издание)` из `config.toml` проверяется, есть ли `feature.tech` в этой ячейке `conf.json`/`contrib.json`. Если да — ключ выставляется в `true`. Иначе ключа просто нет (не пишем `false`).
2. **Preserved** — всё остальное, что было в `feature.matrix` исходного `descriptions.json`. Прежде всего это маркеры `pg-NN` («уже доступно в апстримном PostgreSQL N»), но это может быть и любой ваш ручной ключ. Эти ключи скрипт **никогда не трогает**: ни не удаляет, ни не перезаписывает.

Порядок ключей в итоговом `matrix`:

1. сначала computed (в порядке версий из `config.toml`, внутри версии — в порядке изданий);
2. затем `pg-*` (по убыванию мажорной версии: `pg-18`, `pg-17`, `pg-16`);
3. затем все прочие preserved ключи в том порядке, в каком они лежали в `descriptions.json`.

Это значит, что прогон `compare_json.py` **идемпотентен**: повторный запуск не плодит дубликатов и не теряет `pg-*`.

### Что попадает в ячейку

«`tech` присутствует в ячейке `(версия, издание)`» означает, что `tech` встретился в одном из трёх источников этой пары:

- `editions[<edition>].patches` в `conf.json`;
- ключах `editions[<edition>].utils` в `conf.json`;
- `contrib[*]` в `contrib.json`, у которых текущее издание (после нормализации `-`/`_`/регистра, как в `compare.py`) присутствует в `contrib[*].editions`.

Перед сравнением имена нормализуются по таблице `[tech_aliases]` (см. ниже).

## Файл `config.toml`

`compare_json.py` использует ту же шапку `[versions."<номер>"]`, что и `compare.py`, **плюс** три дополнительных раздела. `compare.py` эти разделы игнорирует, так что один `config.toml` спокойно живёт для обоих скриптов.

```toml
[edition_display_names]
be          = "Basic Edition"
se          = "Special Edition"
se1c        = "Special Edition 1C"
certified   = "Certified исп. 1"
certified_2 = "Certified исп. 2"
free        = "Free Edition"

[tech_aliases]
"func/xid64mark1"                          = "func/xid64"
"perf/slru_xid64"                          = "perf/slru"
"perf/1c_groupby_alternative_paths/fix/1c" = "perf/1c_groupby_alternative_paths/fix"
"pg_wait_profile"                          = "pg_sample_profile"

[ignored_tech]
"fix/common"   = "общий fix-патч, не отдельная фича"
"func/version" = "служебное"

[versions."18"]
editions = ["be", "se", "se1c"]
conf     = "/home/kot/repo/tantor-db-18_1/tantor/conf.json"
contrib  = "/home/kot/repo/tantor-db-18_1/tantor/contrib/contrib.json"
# ...
```

### `[edition_display_names]` — короткое → человекочитаемое имя

В `conf.json` издания называются `be`, `se`, `se1c`, `certified`, `certified_2`, `free`. В `Example.json` нужны более читаемые имена — *Basic Edition*, *Certified исп. 1* и т.д. Маппинг задаётся явно здесь. Если ключа в таблице нет, в `Example.json` подставляется короткое имя как есть.

> Автоматически вывести читаемое имя из `conf.json` нельзя: и `certified`, и `certified_2` имеют там одинаковое `name = "Tantor Certified Edition"`.

### `[tech_aliases]` — алиас → канон

Ключ — техническое имя из `conf.json`/`contrib.json`, значение — каноническое имя, под которым фича лежит в `descriptions.json`. Применяется в двух местах:

1. при сборке `matrix` алиас и канон считаются одной фичей (например, `func/xid64mark1` в 18-й версии «закрывает» фичу `func/xid64`);
2. такой алиас **не попадает** в отчёт «patches/utils/contribs missing from descriptions.json» в конце прогона.

### `[ignored_tech]` — служебные tech, которые не «фичи»

Ключ — техническое имя (можно указывать как алиас, так и канон), значение — свободный комментарий «почему игнорируем», скрипт его не читает. Используется только для отчёта в конце прогона: имена из этой таблицы **не показываются** как «не описано в `descriptions.json`». На сборку `matrix` это не влияет.

Поддерживается **только точное совпадение** имени. Например, `"func/sysviews_out_test"` не закрывает `func/sysviews_out_test/1c`, их нужно перечислить отдельно.

## Файл `descriptions.json`

Главный источник «человеческого» содержания. Файл **обязательный** — без него скрипт сразу падает с ошибкой.

```json
{
  "groups": [
    {
      "name": "Улучшения и оптимизация ядра",
      "features": [
        {
          "name": "64-битный счетчик транзакций (XID)",
          "tech": "func/xid64",
          "description": "...",
          "matrix": {},
          "doc_url": "https://docs.tantorlabs.ru/..."
        }
      ]
    }
  ]
}
```

Поля фичи:

| поле          | обязательное | назначение |
|---------------|:-:|---|
| `name`        | да | человекочитаемое имя фичи (для конечной таблицы / `Example.json`) |
| `tech`        | да | один tech-идентификатор: имя патча из `conf.json:editions[*].patches`, ключ из `conf.json:editions[*].utils`, или `contrib[*].name` из `contrib.json` |
| `description` | да | свободный текст; допускается Markdown (рендерится уже на стороне потребителя `Example.json`) |
| `matrix`      | да | объект ручных меток (`pg-18`, `pg-17`, …). Допустим пустой `{}` — тогда `compare_json.py` сам заполнит только computed-ключи |
| `doc_url`     | нет | ссылка на документацию; копируется в `Example.json` как есть |

Порядок групп и фич в `descriptions.json` сохраняется в `Example.json`.

### Ключ `tech` — только строка

`tech` хранится **как одна строка**, не массив. Если фактически одна и та же фича в разных версиях называется по-разному (`func/xid64` в 14–17, `func/xid64mark1` в 18), нужно:

1. в `descriptions.json` указать каноническое имя (`func/xid64`);
2. в `config.toml` в `[tech_aliases]` добавить пару `"func/xid64mark1" = "func/xid64"`.

### Поле `matrix` — что туда писать руками

Только то, что скрипт **не умеет вычислить сам**. На практике это маркеры `pg-NN` («это уже в апстримном PostgreSQL NN»). Их нет ни в `conf.json`, ни в `contrib.json`, поэтому никакой автоматики нет — заполняется вручную.

`compare_json.py` распознаёт «свои» ключи как `"<известная версия из config.toml>-<что-угодно>"` и пересчитывает их. Всё остальное (включая `pg-*`) переносится в итоговый `Example.json` без изменений.

## Интерактивный вопрос при запуске

Скрипт задаёт **один** вопрос (Enter → значение по умолчанию; `y`/`yes`/`да`/`д` или `n`/`no`/`нет`/`н`; при закрытом stdin — значение по умолчанию):

```
Использовать пути json из config.toml? [Y/n]:
```

- **yes** — берём `conf.json` / `contrib.json` по абсолютным путям из `config.toml`.
- **no** — пути из `config.toml` **игнорируются**; для каждой версии скрипт читает файлы из папки `<директория скрипта>/<номер версии>/`:

  ```
  compare_table/
    compare_json.py
    config.toml
    descriptions.json
    14/conf.json
    14/contrib.json
    ...
    18/conf.json
    18/contrib.json
  ```

  Имя папки должно совпадать с ключом версии в `config.toml` (`"18"` ↔ `18/`).

После выбора в stderr печатается:

```
источник json: пути из config.toml
# или
источник json: папки версий рядом со скриптом (/home/kot/repo-personal/compare_table)
```

## Валидация конфигурации

Перед сборкой `Example.json` скрипт строго проверяет конфигурацию и при ошибках завершается с кодом `2`. Все найденные проблемы выводятся одним списком, чтобы их можно было поправить за один проход:

```
error: config validation failed (3 problem(s)):
  - version '18': edition(s) ['ghost_edition'] not found in /path/to/conf.json (available: ['be', 'certified', 'certified_2', 'free', 'se', 'se1c'])
  - version '17': contrib.json not found: /path/that/does/not/exist.json
  - version '13': missing required key(s): editions, contrib
```

Что проверяется:

1. В `config.toml` есть хотя бы одна секция `[versions.*]`.
2. В каждой такой секции указаны три обязательных ключа: `editions`, `conf`, `contrib`.
3. `editions` — непустой список строк.
4. Файлы `conf.json` и `contrib.json` существуют и являются валидным JSON-объектом.
5. Каждое издание из `editions` действительно присутствует в верхнеуровневом объекте `editions` соответствующего `conf.json` (если издание не найдено, в сообщении выводится список реально доступных).

Дополнительно перед валидацией: TOML-парсер `config.toml` запрещает дубли ключей в одной таблице (например, в `[tech_aliases]`). Такая ошибка приходит ещё раньше как `cannot parse config.toml: Cannot overwrite a value (at line N, column M)`.

## Отчёты в конце прогона

После записи `Example.json` скрипт печатает **три симметричных блока**:

```
Патчи из conf.json без записи в descriptions.json (все версии из config.toml) — 0, игнорируется по [ignored_tech]: 23:
  (все патчи описаны)

Утилиты из conf.json без записи в descriptions.json (все версии из config.toml) — 3:
  - columnar_migrator   (in: 18, 17, 16)
  - pgcompacttable      (in: 18, 17, 16, 15, 14)
  - slru_upgrader       (in: 18, 17, 16)

Расширения из contrib.json без записи в descriptions.json (все версии из config.toml) — 0:
  (все расширения описаны)
```

Для каждого блока:

- слева — имя tech, справа — список версий из `config.toml`, в `conf.json`/`contrib.json` которых это имя встречается;
- алиасы из `[tech_aliases]` уже свёрнуты к каноническому имени;
- имена из `[ignored_tech]` исключены, и в шапке блока показано, сколько ещё имён в принципе отфильтровано через эту таблицу;
- порядок версий в `(in: …)` — как в `config.toml` (самая свежая первой).

Дополнительно скрипт может вывести блок «Фичи с пустой matrix» — это фичи из `descriptions.json`, чей `tech` не нашёлся ни в одной паре `(версия, издание)` и не имеет ручных `pg-*`. Чаще всего это симптом опечатки в `tech` или отсутствия `[tech_aliases]`.

В самом низу — короткая сводка:

```
OK: 5 версий, 7 групп, 97 фич
  -> Example.json
```

## Запуск

### Из терминала

```bash
cd /home/kot/repo-personal/compare_table
./compare_json.py                              # дефолты
./compare_json.py --out Example.next.json
./compare_json.py --missing-from 18,17         # ограничить отчёт по версиям
```

CLI-аргументы:

- `--config PATH`        — путь до `config.toml` (по умолчанию `./config.toml`);
- `--descriptions PATH`  — путь до `descriptions.json` (по умолчанию `./descriptions.json`; **обязательный файл**);
- `--out PATH`           — куда писать результат (по умолчанию `./Example.json`);
- `--missing-from V[,V…]`— ограничить отчёт «не описано в `descriptions.json`» подмножеством версий из `config.toml` (по умолчанию — все).

Относительные пути в этих аргументах резолвятся **относительно директории скрипта**, потому что в начале работы `compare_json.py` делает `os.chdir` в свою папку (нужно для запуска из файлового менеджера, где cwd — домашняя папка).

### Через файловый менеджер

У `compare_json.py` есть шебанг и установлен бит исполнения, поэтому в Nautilus / Nemo / Caja в контекстном меню по правой кнопке мыши доступен пункт **«Запустить как программу»**.

Один раз в файловом менеджере включите запуск исполняемых текстовых файлов (в Nautilus: `Settings → Behavior → Executable Text Files → Run them`), затем — правый клик по `compare_json.py` → **«Запустить как программу»**.

Терминал откроется, скрипт спросит про источник json, отработает, напечатает отчёты и в конце дождётся `Enter` — чтобы окно не схлопнулось мгновенно и можно было прочитать вывод.

## Что лежит в репозитории и что нет

`.gitignore` устроен «инверсно» — всё в директории игнорируется, кроме явно перечисленных:

```
/*
!compare.py
!compare_json.py
!config.toml
!README.md
!descriptions.toml
!descriptions.json
```

То есть в git попадают только эти файлы. Папки версий (`14/`–`18/`) и сгенерированные `Example.json` / `comparison.md` / `comparison.html` — локальные артефакты и в репозиторий не коммитятся.

## Требования

- **Python 3.11+** — используется stdlib-модуль `tomllib`.
- **Python 3.7–3.10** — тоже подходит, но нужно установить пакет [`tomli`](https://pypi.org/project/tomli/):

  ```bash
  pip install tomli
  ```

  Скрипт сам делает `try: import tomllib / except ModuleNotFoundError: import tomli as tomllib`.

Внешних зависимостей, кроме `tomli` (только на старом Python), нет — всё на стандартной библиотеке.

## Коды выхода

| код | смысл                                                                                |
|-----|--------------------------------------------------------------------------------------|
| `0` | успех, `Example.json` записан                                                        |
| `1` | непредвиденное исключение (в stderr напечатан traceback)                             |
| `2` | проблема со входом: нет `config.toml`/`descriptions.json`, битый TOML/JSON, ошибка валидации |

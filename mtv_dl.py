#!/usr/bin/env python3
# coding: utf-8

# noinspection SpellCheckingInspection
"""MediathekView-Commandline-Downloader

Usage:
  {cmd} list [options] [--sets=<file>] [--count=<results>] [<filter>...]
  {cmd} dump [options] [--sets=<file>] [<filter>...]
  {cmd} download [options] [--sets=<file>] [--low|--high] [<filter>...]
  {cmd} history [options] [--reset|--remove=<hash>]

Commands:
  list                                  Show the list of query results as ascii table.
  dump                                  Show the list of query results as json list.
  history                               Show the list of downloaded shows.
  download                              Download shows in the list of query results.

Options:
  -v, --verbose                         Show more details.
  -q, --quiet                           Hide everything not really needed.
  -b, --no-bar                          Hide the progressbar.
  --description-width=<cols>            Maximum width (in columns) of the description
                                        in the progress bar. Default is no limit.
  -l <path>, --logfile=<path>           Log messages to a file instead of stdout.
  -r <hours>, --refresh-after=<hours>   Update database if it is older then the given
                                        number of hours. [default: 3]
  -d <path>, --dir=<path>               Directory to put the databases in (default is
                                        the current working directory).
  --include-future                      Include shows that have not yet started.
  -c <path>, --config=<path>            Path to the config file.

List options:
  -c <results>, --count=<results>       Limit the number of results. [default: 50]

History options:
  --reset                               Reset the list of downloaded shows.
  --remove=<hash>                       Remove a single show from the history.

Download options:
  -h, --high                            Download best available version.
  -l, --low                             Download the smallest available version.
  -o, --oblivious                       Download even if the show alredy is marked as downloaded.
  -t, --target=<path>                   Directory to put the downloaded files in. May contain
                                        the parameters {{dir}} (from the option --dir),
                                        {{filename}} (from server filename) and {{ext}} (file
                                        name extension including the dot), and all fields from
                                        the listing plus {{date}} and {{time}} (the single parts
                                        of {{start}}).
                                        [default: {{dir}}/{{channel}}/{{topic}}/{{start}} {{title}}{{ext}}]
  --mark-only                           Do not download any show, but mark it as downloaded
                                        in the history. This is to initialize a new filter
                                        if upcoming shows are wanted.
  --no-subtitles                        Do not try to download subtitles.
  -s <file>, --sets=<file>              A file to load different sets of filters (see below
                                        for details). In the file every different filter set
                                        is expected to be on a new line.

  WARNING: Please be aware that ancient RTMP streams are not supported
           They will not even get listed.

Filters:

  Use filter to select only the shows wanted. Syntax is always <field><operator><pattern>.

  The following operators and fields are available:

   '='  Pattern is a search within the field value. It's a case insensitive regular expression
        for the fields 'description', 'start', 'region', 'size', 'channel', 'topic', 'title',
        'hash' and 'url'. For the fields 'duration' and 'age' it's a basic equality
        comparison.

   '!=' Inverse of the '=' operator.

   '+'  Pattern must be greater then the field value. Available for the fields 'duration',
        'age', 'start' and 'size'.

   '-'  Pattern must be less then the field value. Available for the same fields as for
        the '+' operator.

  Pattern should be given in the same format as shown in the list command. Times (for
  'start'), time deltas (for 'duration', 'age') and numbers ('size') are parsed and
  smart compared.

  Examples:
    - topic='extra 3'                   (topic contains 'extra 3')
    - title!=spezial                    (title not contains 'spezial')
    - channel=ARD                       (channel contains ARD)
    - age-1mm                           (age is older then 1 month)
    - duration+20m                      (duration longer then 20 min)
    - start+2017-07-01                  (show started after 2017-07-01)
    - start-2017-07-05T23:00:00+02:00   (show started before 2017-07-05, 23:00 CEST)

  As many filters as needed may be given as separated arguments (separated  with space).
  For a show to get considered, _all_ given filter criteria must met.

Filter sets:

  In commandline with a single run one can only give one set of filters. In most cases
  this means one can only select a single show to list or download with one run.

  For --sets, a file should be given, where every line contains the same filter arguments
  that one would give on the commandline. The lines are filtered one after another and
  then processed together. Lines starting with '#' are treated as comment.

  A text file could look for example like this:

    channel=ARD topic='extra 3' title!=spezial duration+20m
    channel=ZDF topic='Die Anstalt' duration+45m
    channel=ZDF topic=heute-show duration+20m

  If additional filters where given through the commandline, all filter sets are extended
  by these filters. Be aware that this is not faster then running all queries separately
  but just more comfortable.

Config file:

  The config file is an optional, yaml formatted text file, that allows to overwrite the most
  arguments by their name. If not defined differently, it is expected to be in the root of
  the home dir ({config_file}). Valid config keys are:

    {config_options}

  Example config:

    verbose: true
    high: true
    dir: ~/download

 """

import codecs
import hashlib
import json
import logging
import lzma
import os
import platform
import random
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from itertools import chain
from pathlib import Path
from textwrap import fill as wrap
from typing import Any
from typing import Dict
from typing import Generator
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

import docopt
import durationpy
import iso8601
import requests
import rfc6266
import tzlocal
import yaml
from bs4 import BeautifulSoup
from pydash import py_
from terminaltables import AsciiTable
from tinydb import Query as TinyQuery
from tinydb import TinyDB
from tinydb_serialization import SerializationMiddleware as TinySerializationMiddleware
from tinydb_serialization import Serializer as TinySerializer
from tqdm import tqdm
from typing_extensions import TypedDict
from yaml.error import YAMLError

CHUNK_SIZE = 128 * 1024

HIDE_PROGRESSBAR = True
DESCRIPTION_THRESHOLD = None

# noinspection SpellCheckingInspection
FIELDS = {
    'Beschreibung': 'description',
    'Datum': 'date',
    'DatumL': 'start',
    'Dauer': 'duration',
    'Geo': 'region',
    'Größe [MB]': 'size',
    'Sender': 'channel',
    'Thema': 'topic',
    'Titel': 'title',
    'Url': 'url',
    'Url HD': 'url_hd', 'Url History': 'url_history',
    'Url Klein': 'url_small',
    'Url RTMP': 'url_rtmp',
    'Url RTMP HD': 'url_rtmp_hd',
    'Url RTMP Klein': 'url_rtmp_small',
    'Url Untertitel': 'url_subtitles',
    'Website': 'website',
    'Zeit': 'time',
    'neu': 'new'
}

FILMLISTE_DATABASE_FILE = '.Filmliste.{script_version}.sqlite'

DEFAULT_CONFIG_FILE = Path('~/.mtv_dl.yml')
CONFIG_OPTIONS = {
    'count': int,
    'dir': str,
    'description-width': int,
    'high': bool,
    'include-future': bool,
    'logfile': str,
    'low': bool,
    'no-bar': bool,
    'no-subtitles': bool,
    'quiet': bool,
    'refresh-after': int,
    'target': str,
    'verbose': bool
}


# see https://res.mediathekview.de/akt.xml
DATABASE_URLS = [
    "https://liste.mediathekview.de/Filmliste-akt.xz",
    "http://verteiler1.mediathekview.de/Filmliste-akt.xz",
    "http://verteiler2.mediathekview.de/Filmliste-akt.xz",
    "http://verteiler3.mediathekview.de/Filmliste-akt.xz"
    "http://verteiler4.mediathekview.de/Filmliste-akt.xz",
    "http://verteiler5.mediathekview.de/Filmliste-akt.xz",
    "http://verteiler6.mediathekview.de/Filmliste-akt.xz",
    "http://download10.onlinetvrecorder.com/mediathekview/Filmliste-akt.xz",
]

logger = logging.getLogger('mtv_dl')
local_zone = tzlocal.get_localzone()
now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)


def trim_description(s: str, length=...) -> str:
    if length is ...:
        length=DESCRIPTION_THRESHOLD
    if length and len(s) > length:
        return f"{s[:DESCRIPTION_THRESHOLD]}…"
    return s


# add timedelta database type
sqlite3.register_adapter(timedelta, lambda v: v.total_seconds())
sqlite3.register_converter("timedelta", lambda v: timedelta(seconds=int(v)))


# noinspection PyClassHasNoInit
class DateTimeSerializer(TinySerializer):

    OBJ_CLASS = datetime

    def encode(self, obj):
        return obj.strftime('%Y-%m-%dT%H:%M:%S')

    def decode(self, s):
        return datetime.strptime(s, '%Y-%m-%dT%H:%M:%S')


# noinspection PyClassHasNoInit
class TimedeltaSerializer(TinySerializer):

    OBJ_CLASS = timedelta

    def encode(self, obj):
        return str(obj.total_seconds())

    def decode(self, s):
        return timedelta(seconds=float(s))


class ConfigurationError(Exception):
    pass


def serialize_for_json(obj: Any) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, timedelta):
        return str(obj)
    else:
        raise TypeError('%r is not JSON serializable' % obj)


if platform.system() == 'Windows':
    INVALID_CHARS = '<>:"/\\|?*' + "".join(chr(i) for i in range(32))
else:
    INVALID_CHARS = '/\0'

invalidre = re.compile("[{}]".format(re.escape(INVALID_CHARS)))


def escape_path(s):
    return invalidre.sub("_", s)


class Database(object):

    class Item(TypedDict):
        hash: str
        channel: str
        description: str
        region: str
        size: int
        title: str
        topic: str
        website: str
        new: bool
        url_http: Optional[str]
        url_http_hd: Optional[str]
        url_http_small: Optional[str]
        url_subtitles: str
        start: datetime
        duration: timedelta
        age: timedelta
        downloaded: Optional[bool]

    @property
    def user_version(self) -> int:
        cursor = self.connection.cursor()
        return cursor.execute('PRAGMA user_version;').fetchone()[0]

    def initialize(self) -> None:
        cursor = self.connection.cursor()
        cursor.execute("PRAGMA database_list")
        database_file = cursor.fetchone()[2]
        logger.debug('Initializing database in %r', database_file)
        try:
            cursor.execute("""        
                CREATE TABlE show (
                    hash TEXT,
                    channel TEXT,
                    description TEXT,
                    region TEXT,
                    size INTEGER,
                    title TEXT,
                    topic TEXT,
                    website TEXT,
                    new BOOLEAN,
                    url_http TEXT,
                    url_http_hd TEXT,
                    url_http_small TEXT,
                    url_subtitles TEXT,
                    start TIMESTAMP,
                    duration TIMEDELTA,
                    age TIMEDELTA
                );
            """)
        except sqlite3.OperationalError:
            cursor.execute("DELETE TABLE show")
        cursor.execute(f'PRAGMA user_version={int(now.timestamp())}')
        self.connection.commit()

        # get show data
        cursor.executemany("""
            INSERT INTO show
            VALUES (
                :hash,
                :channel,
                :description,
                :region,
                :size,
                :title,
                :topic,
                :website,
                :new,
                :url_http,
                :url_http_hd,
                :url_http_small,
                :url_subtitles,
                :start,
                :duration,
                :age
            ) 
        """, self._get_shows())

        self.connection.commit()

    def __init__(self, database: Path) -> None:
        database_path = database.parent / database.name.format(script_version=self._script_version)
        logger.debug('Opening database %r', database_path)
        self.connection = sqlite3.connect(database_path.absolute().as_posix(),
                                          detect_types=sqlite3.PARSE_DECLTYPES,
                                          timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.create_function("REGEXP", 2,
                                        lambda expr, item: re.compile(expr, re.IGNORECASE).search(item) is not None)
        if self.user_version == 0:
            self.initialize()

    @staticmethod
    def _qualify_url(basis: str, extension: str) -> Union[str, None]:
        if extension:
            if '|' in extension:
                offset, text = extension.split('|', maxsplit=1)
                return basis[:int(offset)] + text
            else:
                return basis + extension
        else:
            return None

    @staticmethod
    def _duration_in_seconds(duration: str) -> int:
        if duration:
            match = re.match(r'(?P<h>\d+):(?P<m>\d+):(?P<s>\d+)', duration)
            if match:
                parts = match.groupdict()
                return int(timedelta(hours=int(parts['h']),
                                     minutes=int(parts['m']),
                                     seconds=int(parts['s'])).total_seconds())
        return 0

    @staticmethod
    def _show_hash(channel: str, topic: str, title: str, size: int, start: datetime) -> str:
        h = hashlib.sha1()
        h.update(channel.encode())
        h.update(topic.encode())
        h.update(title.encode())
        h.update(str(size).encode())
        h.update(str(start.timestamp()).encode())
        return h.hexdigest()

    @contextmanager
    def _showlist(self, retries: int = len(DATABASE_URLS)) -> Generator[Path, None, None]:
        while retries:
            retries -= 1
            try:
                url = random.choice(DATABASE_URLS)
                logger.debug('Opening database from %r.', url)
                response = requests.get(url, stream=True)
                response.raise_for_status()
                total_size = int(response.headers.get('content-length', 0))  # type: ignore
                fd, fname = tempfile.mkstemp(prefix='.tmp', suffix='.xz')
                try:
                    with os.fdopen(fd, 'wb', closefd=False) as fh:
                        with tqdm(total=total_size,
                                  unit='B',
                                  unit_scale=True,
                                  leave=False,
                                  disable=HIDE_PROGRESSBAR,
                                  desc=trim_description('Downloading database')) as progress_bar:
                            for data in response.iter_content(CHUNK_SIZE):
                                progress_bar.update(len(data))
                                fh.write(data)
                    yield Path(fname)
                finally:
                    os.close(fd)
                    os.remove(fname)
            except requests.exceptions.HTTPError as e:
                if retries:
                    logger.debug('Database download failed (%d more retries): %s' % (retries, e))
                else:
                    logger.error('Database download failed (no more retries): %s' % e)
                    raise requests.exceptions.HTTPError('retry limit reached, giving up')
                time.sleep(5)
            else:
                break

    @property
    def _script_version(self) -> float:
        return int(Path(__file__).stat().st_mtime)

    def _get_shows(self) -> Iterable["Database.Item"]:
        meta: Dict = {}
        header: List = []
        channel, topic, region = '', '', ''
        with self._showlist() as showlist_path:
            with lzma.open(showlist_path, 'rt', encoding='utf-8') as fh:

                logger.debug('Loading database items.')
                for p in tqdm(json.load(fh, object_pairs_hook=lambda _pairs: _pairs),  # type: ignore
                              unit='shows',
                              leave=False,
                              disable=HIDE_PROGRESSBAR,
                              desc=trim_description('Reading database items')):

                    if not meta and p[0] == 'Filmliste':
                        meta = {
                            # p[1][0] is local date, p[1][1] is gmt date
                            'date': datetime.strptime(p[1][1], '%d.%m.%Y, %H:%M').replace(tzinfo=timezone.utc),
                            'crawler_version': p[1][2],
                            'crawler_agent': p[1][3],
                            'list_id': p[1][4],
                        }

                    elif p[0] == 'Filmliste':
                        if not header:
                            header = p[1]
                            for i, h in enumerate(header):
                                header[i] = FIELDS.get(h, h)

                    elif p[0] == 'X':
                        show = dict(zip(header, p[1]))
                        channel = show.get('channel') or channel
                        topic = show.get('topic') or topic
                        region = show.get('region') or region
                        if show['start'] and show['url'] and show['size']:
                            title = show['title']
                            size = int(show['size']) if show['size'] else 0
                            start = datetime.fromtimestamp(int(show['start']), tz=timezone.utc).replace(tzinfo=None)
                            duration = timedelta(seconds=self._duration_in_seconds(show['duration']))
                            yield {
                                'hash': self._show_hash(channel, topic, title, size, start),
                                'channel': channel,
                                'description': show['description'],
                                'region': region,
                                'size': size,
                                'title': title,
                                'topic': topic,
                                'website': show['website'],
                                'new': show['new'] == 'true',
                                'url_http': str(show['url']) or None,
                                'url_http_hd': self._qualify_url(show['url'], show['url_hd']),
                                'url_http_small': self._qualify_url(show['url'], show['url_small']),
                                'url_subtitles': show['url_subtitles'],
                                'start': start,
                                'duration': duration,
                                'age': now.replace(tzinfo=None)-start,
                                'downloaded': None,
                            }

    def initialize_if_old(self, refresh_after):
        database_age = now - datetime.fromtimestamp(self.user_version, tz=timezone.utc)
        if database_age > timedelta(hours=refresh_after):
            logger.debug('Database age is %s (too old).', database_age)
            self.initialize()
        else:
            logger.debug('Database age is %s.', database_age)

    @staticmethod
    def read_filter_sets(sets_file_path: Path, default_filter):
        if sets_file_path:
            with sets_file_path.expanduser().open('r+') as set_fh:
                for line in set_fh:
                    if line.strip() and not re.match(r'^\s*#', line):
                        yield default_filter + shlex.split(line)
        else:
            yield default_filter

    def filtered(self,
                 rules: List[str],
                 include_future: bool = False,
                 limit = Optional[int]) -> Iterable["Database.Item"]:

        where = []
        arguments: List[Any] = []
        if rules:
            logger.debug('Applying filter: %s', ', '.join(rules))

            for f in rules:
                match = re.match(r'^(?P<field>\w+)(?P<operator>(?:=|!=|\+|-|\W+))(?P<pattern>.*)$', f)
                if match:
                    field, operator, pattern = match.group('field'), \
                                               match.group('operator'), \
                                               match.group('pattern')  # type: str, str, Any

                    # replace odd names
                    field = {
                        'url': 'url_http'
                    }.get(field, field)

                    if operator == '=':
                        if field in ('description', 'region', 'size', 'channel',
                                     'topic', 'title', 'hash', 'url_http'):
                            where.append(f"{field} REGEXP ?")
                            arguments.append(str(pattern))
                        elif field in ('duration', 'age'):
                            where.append(f"{field}=?")
                            arguments.append(durationpy.from_str(pattern).total_seconds())
                        elif field in ('start',):
                            where.append(f"{field}=?")
                            arguments.append(iso8601.parse_date(pattern).isoformat())
                        else:
                            raise ConfigurationError('Invalid operator %r for %r.' % (operator, field))

                    elif operator == '!=':
                        if field in ('description', 'region', 'size', 'channel',
                                     'topic', 'title', 'hash', 'url_http'):
                            where.append(f"{field} NOT REGEXP ?")
                            arguments.append(str(pattern))
                        elif field in ('duration', 'age'):
                            where.append(f"{field}!=?")
                            arguments.append(durationpy.from_str(pattern).total_seconds())
                        elif field in ('start',):
                            where.append(f"{field}!=?")
                            arguments.append(iso8601.parse_date(pattern).isoformat())
                        else:
                            raise ConfigurationError('Invalid operator %r for %r.' % (operator, field))

                    elif operator == '-':
                        if field in ('duration', 'age'):
                            where.append(f"{field}<=?")
                            arguments.append(durationpy.from_str(pattern).total_seconds())
                        elif field == 'size':
                            where.append(f"{field}<=?")
                            arguments.append(int(pattern))
                        elif field == 'start':
                            where.append(f"{field}<=?")
                            arguments.append(iso8601.parse_date(pattern))
                        else:
                            raise ConfigurationError('Invalid operator %r for %r.' % (operator, field))

                    elif operator == '+':
                        if field in ('duration', 'age'):
                            where.append(f"{field}>=?")
                            arguments.append(durationpy.from_str(pattern).total_seconds())
                        elif field == 'size':
                            where.append(f"{field}>=?")
                            arguments.append(int(pattern))
                        elif field == 'start':
                            where.append(f"{field}>=?")
                            arguments.append(iso8601.parse_date(pattern))
                        else:
                            raise ConfigurationError('Invalid operator %r for %r.' % (operator, field))

                    else:
                        raise ConfigurationError('Invalid operator: %r' % operator)

                else:
                    raise ConfigurationError('Invalid filter definition. '
                                             'Property and filter rule expected separated by an operator.')

        if not include_future:
            where.append("date(start) < date('now')")

        query = "SELECT * FROM show "
        if where:
            query += f"WHERE {' AND '.join(where)} "
        if limit:
            query += f"LIMIT {limit} "

        cursor = self.connection.cursor()
        cursor.execute(query, arguments)
        for row in cursor:
            yield dict(row)


class History(object):

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    @property  # type: ignore
    @contextmanager
    def db(self) -> TinyDB:
        history_storage = TinySerializationMiddleware()
        history_storage.register_serializer(DateTimeSerializer(), 'Datetime')
        history_storage.register_serializer(TimedeltaSerializer(), 'Timedelta')
        history_file_path = self._cwd / '.history._db'
        history_file_path.parent.mkdir(parents=True, exist_ok=True)
        db = TinyDB(history_file_path, default_table='history', storage=history_storage)
        yield db
        db.close()

    @property  # type: ignore
    def all(self):
        with self.db as db:
            return db.all()

    def check(self, shows: Iterable[Database.Item]) -> Iterable[Database.Item]:
        row = TinyQuery()
        with self.db as db:
            for item in shows:
                historic_download = db.get(row.hash == item['hash'])
                if historic_download:
                    item['downloaded'] = historic_download['downloaded']
                else:
                    item['downloaded'] = False
                yield item

    def purge(self):
        with self.db as db:
            return db.purge_tables()

    def remove(self, show_hash):
        row = TinyQuery()
        with self.db as db:
            if db.remove(row.hash == show_hash):
                logger.info('Removed %s from history.', show_hash)
                return True
            else:
                logger.warning('Could not remove %s (not found).', show_hash)
                return False

    def insert(self, show):
        with self.db as db:
            return db.insert(show)


class Table(object):

    _default_headers = ['hash',
                        'channel',
                        'title',
                        'topic',
                        'size',
                        'start',
                        'duration',
                        'age',
                        'region',
                        'downloaded']

    def __init__(self, shows: Iterable[Database.Item], headers: List[str] = None) -> None:
        self.headers = headers if isinstance(headers, list) else self._default_headers  # type: List
        # noinspection PyTypeChecker
        self.data = [[self._escape_cell(t, row.get(t)) for t in self.headers] for row in shows]

    @staticmethod
    def _escape_cell(title: str, obj: Any) -> str:
        if title=='hash':
            return str(obj)[:11]
        elif isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, timedelta):
            return re.sub(r'(\d+)', r' \1', durationpy.to_str(obj, extended=True)).strip()
        else:
            return str(obj)

    def as_ascii_table(self):
        return AsciiTable([self.headers] + self.data).table


class Show(dict):

    @property
    def label(self) -> str:
        return "%(title)r [%(channel)s, %(topic)r, %(start)s, %(hash)s]" % self

    def _download_files(self, destination_dir_path: Path, target_urls: List[str]) -> Generator[Path, None, None]:

        file_sizes = []
        with tqdm(unit='B',
                  unit_scale=True,
                  leave=False,
                  disable=HIDE_PROGRESSBAR,
                  desc=trim_description(f'Downloading {self.label}')) as progress_bar:

            for url in target_urls:

                # determine file size for progressbar
                response = requests.get(url, stream=True, timeout=60)
                file_sizes.append(int(response.headers.get('content-length', 0)))  # type: ignore
                progress_bar.total = sum(file_sizes) / len(file_sizes) * len(target_urls)

                # determine file name and destination
                default_filename = os.path.basename(url)
                file_name = rfc6266.parse_requests_response(response).filename_unsafe or default_filename
                destination_file_path = destination_dir_path / file_name

                # actual download
                with destination_file_path.open('wb') as fh:
                    for data in response.iter_content(CHUNK_SIZE):
                        progress_bar.update(len(data))
                        fh.write(data)

                yield destination_file_path

    def _move_to_user_target(self,
                             source_path: Path,
                             cwd: Path,
                             target: Path,
                             file_name: str,
                             file_extension: str,
                             media_type: str):

        escaped_show_details = {k: escape_path(str(v)) for k, v in self.items()}
        destination_file_path = Path(target.as_posix().format(dir=cwd,
                                                              filename=file_name,
                                                              ext=file_extension,
                                                              date=self['start'].date().isoformat(),
                                                              time=self['start'].strftime('%H-%M'),
                                                              **escaped_show_details))

        destination_file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(source_path.as_posix(), destination_file_path)
        except OSError as e:
            logger.warning('Skipped %s. Moving %r to %r failed: %s', self.label, source_path, destination_file_path, e)
        else:
            logger.info('Saved %s %s to %r.', media_type, self.label, destination_file_path)

    @staticmethod
    def _get_m3u8_segments(base_url: str, hls_file_path: Path) -> Generator[Dict[str, Any], None, None]:

        with hls_file_path.open('r+') as fh:
            segment = {}  # type: Dict
            for line in fh:
                if not line:
                    continue
                elif line.startswith("#EXT-X-STREAM-INF:"):
                    # see http://archive.is/Pe9Pt#section-4.3.4.2
                    segment = {m.group(1).lower(): m.group(2).strip() for m in re.finditer(r'([A-Z-]+)=([^,]+)', line)}
                    for key, value in segment.items():
                        if value[0] in ('"', "'") and value[0] == value[-1]:
                            segment[key] = value[1:-1]
                        else:
                            try:
                                segment[key] = int(value)
                            except ValueError:
                                pass
                elif not line.startswith("#"):
                    segment['url'] = urllib.parse.urljoin(base_url, line.strip())
                    yield segment
                    segment = {}

    def _download_hls_target(self,
                             temp_dir_path: Path,
                             base_url: str,
                             quality_preference: Tuple[str, str, str],
                             hls_file_path: Path) -> Path:

        # get the available video streams ordered by quality
        hls_index_segments = py_ \
            .chain(self._get_m3u8_segments(base_url, hls_file_path)) \
            .filter(lambda s: 'mp4a' not in s.get('codecs')) \
            .filter(lambda s: s.get('bandwidth')) \
            .sort(key=lambda s: s.get('bandwidth')) \
            .value()

        # select the wanted stream
        if quality_preference[0] == '_hd':
            designated_index_segment = hls_index_segments[-1]
        elif quality_preference[0] == '_small':
            designated_index_segment = hls_index_segments[0]
        else:
            designated_index_segment = hls_index_segments[len(hls_index_segments) // 2]

        designated_index_file = list(self._download_files(temp_dir_path, [designated_index_segment['url']]))[0]
        logger.debug('Selected HLS bandwidth is %d (available: %s).',
                     designated_index_segment['bandwidth'],
                     ', '.join(str(s['bandwidth']) for s in hls_index_segments))

        # get stream segments
        hls_target_segments = list(self._get_m3u8_segments(base_url, designated_index_file))
        hls_target_files = self._download_files(temp_dir_path, list(s['url'] for s in hls_target_segments))
        logger.debug('%d HLS segments to download.', len(hls_target_segments))

        # download and join the segment files
        temp_file_path = Path(tempfile.mkstemp(dir=temp_dir_path, prefix='.tmp')[1])
        with temp_file_path.open('wb') as out_fh:
            for segment_file_path in hls_target_files:

                with segment_file_path.open('rb') as in_fh:
                    out_fh.write(in_fh.read())

                # delete the segment file immediately to save disk space
                segment_file_path.unlink()

        return temp_file_path

    @staticmethod
    def _convert_subtitles_xml_to_srt(subtitles_xml_path: Path) -> Path:

        subtitles_srt_path = subtitles_xml_path.parent / (subtitles_xml_path.stem + '.srt')
        soup = BeautifulSoup(subtitles_xml_path.read_text(), "html.parser")

        colour_to_rgb = {
            "textBlack": "#000000",
            "textRed": "#FF0000",
            "textGreen": "#00FF00",
            "textYellow": "#FFFF00",
            "textBlue": "#0000FF",
            "textMagenta": "#FF00FF",
            "textCyan": "#00FFFF",
            "textWhite": "#FFFFFF"}

        def font_colour(text, colour):
            return "<font color=\"%s\">%s</font>\n" % (colour_to_rgb[colour], text)

        with subtitles_srt_path.open('w') as srt:
            for p_tag in soup.findAll("tt:p"):
                # noinspection PyBroadException
                try:
                    srt.write(str(int(p_tag.get("xml:id").replace("sub", "")) + 1) + "\n")
                    srt.write(f"{p_tag['begin'].replace('.', ',')} --> {p_tag['end'].replace('.', ',')}\n")
                    for span_tag in p_tag.findAll('tt:span'):
                        srt.write(font_colour(span_tag.text, span_tag.get('style')).replace("&apos", "'"))
                    srt.write('\n\n')
                except Exception:
                    logger.debug('Unexpected data in subtitle xml file: %s', p_tag)

        return subtitles_srt_path

    def __init__(self, show: Dict[str, Any], **kwargs: Dict) -> None:
        super().__init__(show, **kwargs)

    def download(self,
                 quality: Tuple[str, str, str],
                 cwd: Path,
                 target: Path,
                 *,
                 include_subtitles: bool = True) -> Union[Path, None]:
        temp_path = Path(tempfile.mkdtemp(prefix='.tmp'))
        try:

            # show url based on quality preference
            show_url = self["url_http%s" % quality[0]] \
                       or self["url_http%s" % quality[1]] \
                       or self["url_http%s" % quality[2]]

            logger.debug('Downloading %s from %r.', self.label, show_url)
            show_file_path = list(self._download_files(temp_path, [show_url]))[0]
            show_file_name = show_file_path.name
            if '.' in show_file_name:
                show_file_extension = show_file_path.suffix
                show_file_name = show_file_path.stem
            else:
                show_file_extension = ''

            if show_file_extension in ('.mp4', '.flv', '.mp3'):
                self._move_to_user_target(show_file_path, cwd, target, show_file_name, show_file_extension, 'show')

            # TODO: consider to remove hsl/m3u8 downloads ("./mtv_dl.py dump url='[^(mp4|flv|mp3)]$'" is empty)
            elif show_file_extension == '.m3u8':
                ts_file_path = self._download_hls_target(temp_path, show_url, quality, show_file_path)
                self._move_to_user_target(ts_file_path, cwd, target, show_file_name, '.ts', 'show')

            else:
                logger.error('File extension %s of %s not supported.', show_file_extension, self.label)
                return None

            if include_subtitles and self['url_subtitles']:
                logger.debug('Downloading subtitles for %s from %r.', self.label, self['url_subtitles'])
                subtitles_xml_path = list(self._download_files(temp_path, [self['url_subtitles']]))[0]
                subtitles_srt_path = self._convert_subtitles_xml_to_srt(subtitles_xml_path)
                self._move_to_user_target(subtitles_srt_path, cwd, target, show_file_name, '.srt', 'subtitles')

            return show_file_path

        except (requests.exceptions.RequestException, OSError) as e:
            logger.error('Download of %s failed: %s', self.label, e)
        finally:
            shutil.rmtree(temp_path)

        return None


def load_config(arguments: Dict) -> Dict:

    config_file_path = (Path(arguments['--config']) if arguments['--config'] else DEFAULT_CONFIG_FILE).expanduser()

    try:
        config = yaml.safe_load(config_file_path.open())

    except OSError as e:
        if arguments['--config']:
            logger.error('Config file file defined but not loaded: %s', e)
            sys.exit(1)

    except YAMLError as e:
        logger.error('Unable to read config file: %s', e)
        sys.exit(1)

    else:
        invalid_config_options = set(config.keys()).difference(CONFIG_OPTIONS.keys())
        if invalid_config_options:
            logger.error('Invalid config options: %s', ', '.join(invalid_config_options))
            sys.exit(1)

        else:
            for option in config:
                option_type = CONFIG_OPTIONS.get(option)
                if option_type and not isinstance(config[option], option_type):
                    logger.error('Invalid type for config option %r (found %r but %r expected).',
                                 option, type(config[option]).__name__, CONFIG_OPTIONS[option].__name__)
                    sys.exit(1)

        arguments.update({'--%s' % o: config[o] for o in config})

    return arguments


def main():

    # argument handling
    arguments = docopt.docopt(__doc__.format(cmd=Path(__file__).name,
                                             config_file=DEFAULT_CONFIG_FILE,
                                             config_options=wrap(', '.join("%s (%s)" % (c, k.__name__)
                                                                           for c, k in CONFIG_OPTIONS.items()),
                                                                 width=80,
                                                                 subsequent_indent=' ' * 4)))

    # broken console encoding handling  (http://archive.is/FRcJe#60%)
    if sys.stdout.encoding != 'UTF-8':
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    if sys.stderr.encoding != 'UTF-8':
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

    # rfc6266 logger fix (don't expect an upstream fix for that)
    for logging_handler in rfc6266.LOGGER.handlers:
        rfc6266.LOGGER.removeHandler(logging_handler)

    # mute third party modules
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("rfc6266").setLevel(logging.WARNING)

    # ISO8601 logger
    if arguments['--logfile']:
        logging_handler = logging.FileHandler(Path(arguments['--logfile']).expanduser())
    else:
        logging_handler = logging.StreamHandler()

    logging_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
    logger.addHandler(logging_handler)
    sys.excepthook = lambda _c, _e, _t: logger.critical('%s: %s\n%s', _c, _e, ''.join(traceback.format_tb(_t)))

    # config handling
    arguments = load_config(arguments)

    # progressbar handling
    global HIDE_PROGRESSBAR
    global DESCRIPTION_THRESHOLD
    HIDE_PROGRESSBAR = bool(arguments['--logfile']) or bool(arguments['--no-bar']) or arguments['--quiet']
    DESCRIPTION_THRESHOLD = int(arguments['--description-width']) if arguments['--description-width'] else None

    if arguments['--verbose']:
        logger.setLevel(logging.DEBUG)
    elif arguments['--quiet']:
        logger.setLevel(logging.ERROR)
    else:
        logger.setLevel(logging.INFO)

    # temp file and download config
    cw_dir = Path(arguments['--dir']).expanduser().absolute() if arguments['--dir'] else Path(os.getcwd())
    target_dir = Path(arguments['--target']).expanduser()
    cw_dir.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = cw_dir

    #  tracking
    history = History(cwd=cw_dir)

    try:
        if arguments['history']:
            if arguments['--reset']:
                history.purge()
            elif arguments['--remove']:
                history.remove(arguments['--remove'])
            else:
                print(Table(sorted(history.all, key=lambda s: s.get('downloaded'))).as_ascii_table())

        else:
            showlist = Database(database=cw_dir / FILMLISTE_DATABASE_FILE)
            showlist.initialize_if_old(refresh_after=int(arguments['--refresh-after']))

            limit = int(arguments['--count']) if arguments['list'] else None
            shows = history.check(
                chain(*(showlist.filtered(rules=filter_set,
                                          include_future=arguments['--include-future'],
                                          limit=limit or None)
                        for filter_set
                        in showlist.read_filter_sets(sets_file_path=(Path(arguments['--sets'])
                                                                     if arguments['--sets'] else None),
                                                     default_filter=arguments['<filter>'])))
            )

            if arguments['list']:
                print(Table(shows).as_ascii_table())

            elif arguments['dump']:
                print(json.dumps(list(shows), default=serialize_for_json, indent=4, sort_keys=True))

            elif arguments['download']:
                for item in shows:
                    show = Show(item)
                    if not show.get('downloaded') or arguments['--oblivious']:
                        if not arguments['--mark-only']:
                            if arguments['--high']:
                                quality_preference = ('_hd', '', '_small')
                            elif arguments['--low']:
                                quality_preference = ('_small', '', '_hd')
                            else:
                                quality_preference = ('', '_hd', '_small')
                            show.download(quality_preference, cw_dir, target_dir,
                                          include_subtitles=not arguments['--no-subtitles'])
                            item['downloaded'] = now
                            history.insert(item)
                        else:
                            show['downloaded'] = now
                            history.insert(show)
                            logger.info('Marked %s from %s as downloaded.', show.label)
                    else:
                        logger.debug('Skipping %s (already loaded on %s)', show.label, item['downloaded'])

    except ConfigurationError as e:
        logger.error(str(e))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

"""Однотабличная файловая БД и десять дисковых AVL-индексов.

В памяти: заголовок, одна запись, O(log n) кадров дерева.
Строки не собираются в коллекцию для изменения или перезаписи БД.
Журнал undo на диске обеспечивает откат незавершённой операции.
Один процесс/один экземпляр FileDatabase на файл; параллельный доступ запрещён.
"""
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
import os
import shutil
import struct
import tempfile
from contextlib import contextmanager

FIELDS = ['flight_id', 'airline', 'departure_airport', 'arrival_airport',
          'departure_date', 'departure_time', 'aircraft_model', 'ticket_price', 'seats_count']
INDEX_FIELDS = FIELDS + ['composite_key']
SLOT_SIZE = 4096
MAGIC = 'AIRPORT_AVL_3'
LEGACY_MAGIC = 'AIRPORT_AVL_2'
LOG_MAGIC = b'AIRUNDO2'
LOG_HEADER = struct.Struct('<8sQ')
LOG_ADDRESS = struct.Struct('<Q')
LOG_ENTRY_SIZE = LOG_ADDRESS.size + SLOT_SIZE


def _format_date(date):
    return f"{date.day:02d}.{date.month:02d}.{date.year:04d}"


def _date(value):
    return _format_date(dt.datetime.strptime(value, '%d.%m.%Y'))


def _time(value):
    return dt.datetime.strptime(value, '%H:%M').strftime('%H:%M')


def _price(value):
    try:
        number = Decimal(value.replace(',', '.'))
        if not number.is_finite() or number < 0 or number > Decimal('999999999999.99'):
            raise ValueError('Цена вне диапазона 0..999999999999.99.')
        if number != number.quantize(Decimal('.01')):
            raise ValueError('Цена: не более двух знаков после запятой.')
        return format(number, '.2f')
    except InvalidOperation as exc:
        raise ValueError('Некорректная цена.') from exc


def format_value(field, value):
    """Текст только для GUI и индексов; сама запись сохраняет свои типы."""
    if field == 'departure_date' and type(value) is dt.date:
        return _format_date(value)
    if field == 'departure_time' and type(value) is dt.time:
        return value.strftime('%H:%M')
    if field == 'ticket_price' and type(value) is float:
        return format(value, '.2f')
    return str(value)


def validate(record):
    if set(record) != set(FIELDS):
        raise ValueError('В записи должны быть ровно девять полей схемы.')
    if any(v is None for v in record.values()):
        raise ValueError('Заполните все поля.')
    if isinstance(record['departure_time'], dt.time):
        value = record['departure_time']
        if value.second or value.microsecond or value.tzinfo is not None:
            raise ValueError('Время: часы и минуты без секунд и часового пояса.')
    row = {f: (str(record[f]) if f == 'ticket_price' else format_value(f, record[f])).strip() for f in FIELDS}
    if any(not v for v in row.values()):
        raise ValueError('Заполните все поля.')
    for f in FIELDS:
        if len(row[f].encode('utf-8')) > 160:
            raise ValueError('Каждое поле: не более 160 байт UTF-8.')
    try:
        row['departure_date'] = dt.datetime.strptime(row['departure_date'], '%d.%m.%Y').date()
        row['departure_time'] = dt.datetime.strptime(row['departure_time'], '%H:%M').time()
        row['ticket_price'] = float(_price(row['ticket_price']))
        seats = int(row['seats_count'])
        if not 0 <= seats <= 10000:
            raise ValueError('Количество мест: 0..10000.')
        row['seats_count'] = seats
    except (ValueError, OverflowError) as exc:
        raise ValueError('Проверьте дату, время, цену и количество мест. ' + str(exc)) from exc
    row['flight_id'] = row['flight_id'].upper()
    for f in ('departure_airport', 'arrival_airport'):
        row[f] = row[f].upper()
        if len(row[f]) != 3 or not row[f].isascii() or not row[f].isalpha():
            raise ValueError('Код IATA должен содержать три латинские буквы.')
    return row


def composite_key(row):
    return row['flight_id'] + '_' + format_value('departure_date', row['departure_date'])


def normalize_query(field, value):
    if field not in INDEX_FIELDS:
        raise ValueError('Неизвестное поле.')
    value = format_value(field, value).strip()
    if not value or len(value.encode('utf-8')) > 400:
        raise ValueError('Введите значение длиной не более 400 байт.')
    if field == 'composite_key':
        try:
            flight, date = value.rsplit('_', 1)
            if not flight:
                raise ValueError()
            value = flight.upper() + '_' + _date(date)
        except ValueError as exc:
            raise ValueError('Полный ключ: РЕЙС_ДД.ММ.ГГГГ.') from exc
    elif field == 'departure_date':
        value = _date(value)
    elif field == 'departure_time':
        value = _time(value)
    elif field == 'ticket_price':
        value = _price(value)
    elif field == 'seats_count':
        value = str(int(value))
    return value.casefold()


def normalize_prefix(field, value):
    """Префикс может быть неполной датой, числом или составным ключом."""
    if field not in INDEX_FIELDS:
        raise ValueError('Неизвестное поле.')
    value = format_value(field, value).strip()
    if not value or len(value.encode('utf-8')) > 400:
        raise ValueError('Введите начало значения длиной не более 400 байт.')
    if field == 'ticket_price':
        value = value.replace(',', '.')
    # Полностью введённые даты поддерживают привычную нормализацию.
    if field in ('departure_date', 'departure_time', 'composite_key'):
        try:
            value = normalize_query(field, value)
        except ValueError:
            pass
    return value.casefold()


def matches(row, field, value, prefix=False):
    actual = composite_key(row) if field == 'composite_key' else row[field]
    actual = normalize_query(field, actual)
    if prefix:
        return actual.startswith(normalize_prefix(field, value))
    return actual == normalize_query(field, value)


def _json_encode(value):
    # JSON не имеет date/time: сохраняем явную метку типа и ISO-значение.
    if type(value) is dt.date:
        return {'__type__': 'date', 'value': value.isoformat()}
    if type(value) is dt.time:
        return {'__type__': 'time', 'value': value.isoformat()}
    raise TypeError('Неподдерживаемый тип: ' + type(value).__name__)


def _json_decode(value):
    if set(value) == {'__type__', 'value'}:
        if value['__type__'] == 'date':
            return dt.date.fromisoformat(value['value'])
        if value['__type__'] == 'time':
            return dt.time.fromisoformat(value['value'])
        raise ValueError('Неизвестная метка типа.')
    return value


def _pack(obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(',', ':'), default=_json_encode, allow_nan=False).encode('utf-8')
    if len(raw) > SLOT_SIZE:
        raise ValueError('Превышен размер дискового блока.')
    return raw.ljust(SLOT_SIZE, b' ')


class FileDatabase:
    """Слоты: 0 — заголовок, остальные — запись, узел AVL или свободный блок."""
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.journal_path = self.path + '.undo'
        self._file = None
        self._journal = None
        self._recover()
        with open(self.path, 'rb') as file:
            size = os.fstat(file.fileno()).st_size
            raw = file.read(SLOT_SIZE)
        try:
            self.meta = json.loads(raw)
            if (size < SLOT_SIZE or size % SLOT_SIZE or self.meta['magic'] not in (MAGIC, LEGACY_MAGIC)
                    or len(self.meta['roots']) != len(INDEX_FIELDS)
                    or self.meta['count'] < 0 or self.meta['slots'] != size // SLOT_SIZE):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('Неверный или повреждённый формат AVL-БД. Старый .adb не совместим.') from exc

    @property
    def count(self):
        return self.meta['count']

    @classmethod
    def create(cls, path):
        if os.path.exists(str(path) + '.undo'):
            raise ValueError('Рядом остался журнал: сначала восстановите исходную БД.')
        with open(path, 'xb') as file:
            file.write(_pack(dict(magic=MAGIC, roots=[0] * len(INDEX_FIELDS),
                                  count=0, free=0, slots=1)))
            file.flush()
            os.fsync(file.fileno())
        return cls(path)

    def _recover(self):
        if not os.path.exists(self.journal_path):
            return
        if os.path.getsize(self.journal_path) < LOG_HEADER.size:
            # Основной файл не меняется до полной записи заголовка журнала.
            os.unlink(self.journal_path)
            return
        with open(self.journal_path, 'rb') as log:
            magic, original_size = LOG_HEADER.unpack(log.read(LOG_HEADER.size))
            if magic != LOG_MAGIC:
                raise ValueError('Повреждён журнал восстановления.')
            # Незавершённая последняя запись журнала не могла предшествовать записи БД.
            entries = (os.fstat(log.fileno()).st_size - LOG_HEADER.size) // LOG_ENTRY_SIZE
            with open(self.path, 'r+b', buffering=0) as data:
                for i in range(entries - 1, -1, -1):
                    log.seek(LOG_HEADER.size + i * LOG_ENTRY_SIZE)
                    address = LOG_ADDRESS.unpack(log.read(LOG_ADDRESS.size))[0]
                    block = log.read(SLOT_SIZE)
                    data.seek(address)
                    data.write(block)
                data.truncate(original_size)
                os.fsync(data.fileno())
        os.unlink(self.journal_path)

    @contextmanager
    def _transaction(self):
        if self._file is not None:
            raise RuntimeError('Вложенные операции записи запрещены.')
        try:
            self._file = open(self.path, 'r+b', buffering=0)
            self._journal = open(self.journal_path, 'xb', buffering=0)
            self._journal.write(LOG_HEADER.pack(LOG_MAGIC, os.fstat(self._file.fileno()).st_size))
            os.fsync(self._journal.fileno())
            yield
            self.meta['magic'] = MAGIC
            self._put(0, self.meta)
            os.fsync(self._file.fileno())
            self._journal.close()
            self._journal = None
            os.unlink(self.journal_path)  # Точка завершения операции.
        except BaseException:
            if self._journal:
                self._journal.close()
            if self._file:
                self._file.close()
            self._journal = self._file = None
            self._recover()
            with open(self.path, 'rb') as file:
                self.meta = json.loads(file.read(SLOT_SIZE))
            raise
        finally:
            if self._file:
                self._file.close()
            self._file = self._journal = None

    def _get(self, slot):
        if not isinstance(slot, int) or slot <= 0 or slot >= self.meta['slots']:
            raise ValueError('Некорректный номер дискового блока.')
        if self._file is not None:
            self._file.seek(slot * SLOT_SIZE)
            raw = self._file.read(SLOT_SIZE)
        else:
            with open(self.path, 'rb') as file:
                file.seek(slot * SLOT_SIZE)
                raw = file.read(SLOT_SIZE)
        try:
            return json.loads(raw, object_hook=_json_decode)
        except ValueError as exc:
            raise ValueError('Повреждён дисковый блок.') from exc

    def _put(self, slot, obj):
        block = _pack(obj)
        position = slot * SLOT_SIZE
        self._file.seek(position)
        before = self._file.read(SLOT_SIZE).ljust(SLOT_SIZE, b' ')
        self._journal.write(LOG_ADDRESS.pack(position) + before)
        os.fsync(self._journal.fileno())  # Undo обязательно устойчив до изменения БД.
        self._file.seek(position)
        if self._file.write(block) != SLOT_SIZE:
            raise OSError('Неполная запись блока.')

    def _allocate(self, obj):
        if self.meta['free']:
            slot = self.meta['free']
            self.meta['free'] = self._get(slot)['next']
        else:
            slot = self.meta['slots']
            self.meta['slots'] += 1
        self._put(slot, obj)
        return slot

    def _release(self, slot):
        self._put(slot, dict(kind='free', next=self.meta['free']))
        self.meta['free'] = slot

    def _height(self, slot):
        return self._get(slot)['h'] if slot else 0

    def _refresh(self, slot, node):
        node['h'] = 1 + max(self._height(node['l']), self._height(node['r']))
        self._put(slot, node)

    def _rotate_left(self, slot):
        node = self._get(slot)
        top_slot = node['r']
        top = self._get(top_slot)
        node['r'], top['l'] = top['l'], slot
        self._refresh(slot, node)
        self._refresh(top_slot, top)
        return top_slot

    def _rotate_right(self, slot):
        node = self._get(slot)
        top_slot = node['l']
        top = self._get(top_slot)
        node['l'], top['r'] = top['r'], slot
        self._refresh(slot, node)
        self._refresh(top_slot, top)
        return top_slot

    def _balance(self, slot, node):
        self._refresh(slot, node)
        difference = self._height(node['l']) - self._height(node['r'])
        if difference > 1:
            left = self._get(node['l'])
            if self._height(left['l']) < self._height(left['r']):
                node['l'] = self._rotate_left(node['l'])
                self._refresh(slot, node)
            return self._rotate_right(slot)
        if difference < -1:
            right = self._get(node['r'])
            if self._height(right['r']) < self._height(right['l']):
                node['r'] = self._rotate_right(node['r'])
                self._refresh(slot, node)
            return self._rotate_left(slot)
        return slot

    def _insert(self, slot, key):
        if not slot:
            return self._allocate(dict(kind='node', key=list(key), l=0, r=0, h=1))
        node = self._get(slot)
        actual = tuple(node['key'])
        if actual == key:
            raise ValueError('Повтор узла индекса.')
        side = 'l' if key < actual else 'r'
        node[side] = self._insert(node[side], key)
        return self._balance(slot, node)

    def _erase(self, slot, key):
        if not slot:
            raise ValueError('В индексе отсутствует удаляемый узел.')
        node = self._get(slot)
        actual = tuple(node['key'])
        if key != actual:
            side = 'l' if key < actual else 'r'
            node[side] = self._erase(node[side], key)
        else:
            if not node['l'] or not node['r']:
                child = node['l'] or node['r']
                self._release(slot)
                return child
            successor = self._get(node['r'])
            while successor['l']:
                successor = self._get(successor['l'])
            node['key'] = successor['key']
            node['r'] = self._erase(node['r'], tuple(successor['key']))
        return self._balance(slot, node)

    def _lower_bound(self, index, key):
        slot = self.meta['roots'][index]
        answer = None
        while slot:
            node = self._get(slot)
            actual = tuple(node['key'])
            if actual >= key:
                answer = actual
                slot = node['l']
            else:
                slot = node['r']
        return answer

    def _range(self, slot, value):
        if not slot:
            return
        node = self._get(slot)
        actual = node['key'][0]
        if actual >= value:
            yield from self._range(node['l'], value)
        if actual == value:
            yield node['key'][1]
        if actual <= value:
            yield from self._range(node['r'], value)

    @staticmethod
    def _value(row, field):
        return normalize_query(field, composite_key(row) if field == 'composite_key' else row[field])

    def find(self, key):
        value = normalize_query('composite_key', key)
        candidate = self._lower_bound(len(FIELDS), (value, 0))
        if candidate is None or candidate[0] != value:
            return None
        return candidate[1], self.read(candidate[1])

    def read(self, slot):
        block = self._get(slot)
        if block['kind'] != 'row':
            return None
        row = block['row']
        # Совместимость с прежней AVL-версией: преобразуем одну строку при чтении.
        # Индексные значения остаются прежними, перестройка деревьев не нужна.
        if type(row['departure_date']) is int:
            return dict(row, ticket_price=float(Decimal(row['ticket_price']) / 100),
                        departure_date=dt.date.fromordinal(row['departure_date']),
                        departure_time=dt.time(row['departure_time'] // 60, row['departure_time'] % 60))
        return row

    @staticmethod
    def _row_block(row):
        return dict(kind='row', row=row)

    def add(self, record):
        row = validate(record)
        if self.find(composite_key(row)) is not None:
            raise ValueError('Рейс на эту дату уже существует.')
        with self._transaction():
            slot = self._allocate(self._row_block(row))
            for i, field in enumerate(INDEX_FIELDS):
                self.meta['roots'][i] = self._insert(self.meta['roots'][i], (self._value(row, field), slot))
            self.meta['count'] += 1
        return slot

    def update(self, old_key, record):
        row = validate(record)
        found = self.find(old_key)
        if found is None:
            raise ValueError('Старая запись не найдена.')
        slot, old = found
        duplicate = self.find(composite_key(row))
        if duplicate is not None and duplicate[0] != slot:
            raise ValueError('Рейс на эту дату уже существует.')
        with self._transaction():
            for i, field in enumerate(INDEX_FIELDS):
                before, after = self._value(old, field), self._value(row, field)
                if before != after:
                    root = self._erase(self.meta['roots'][i], (before, slot))
                    self.meta['roots'][i] = self._insert(root, (after, slot))
            self._put(slot, self._row_block(row))
        return slot

    def delete_key(self, key):
        found = self.find(key)
        if found is None:
            return None
        slot, row = found
        with self._transaction():
            for i, field in enumerate(INDEX_FIELDS):
                self.meta['roots'][i] = self._erase(self.meta['roots'][i], (self._value(row, field), slot))
            self._release(slot)
            self.meta['count'] -= 1
        return slot

    def _prefix_range(self, slot, low, high):
        if not slot:
            return
        node = self._get(slot)
        value = node['key'][0]
        if value >= low:
            yield from self._prefix_range(node['l'], low, high)
        if value >= low and (high is None or value < high):
            yield node['key'][1]
        if high is None or value < high:
            yield from self._prefix_range(node['r'], low, high)

    def search(self, field, value, prefix=False, limit=None):
        if limit is not None and limit < 0:
            raise ValueError("Лимит не может быть отрицательным.")

        if prefix:
            source = self.search_prefix(field, value)
        else:
            value = normalize_query(field, value)
            root = self.meta["roots"][INDEX_FIELDS.index(field)]

            source = (
                (slot, self.read(slot))
                for slot in self._range(root, value)
            )

        count = 0

        for row in source:
            if limit is not None and count >= limit:
                return

            yield row
            count += 1

    def search_prefix(self, field, value):
        """Диапазон [prefix, successor(prefix)): O(log n + k), без сканирования."""
        low = normalize_prefix(field, value)
        high = None
        for i in range(len(low) - 1, -1, -1):
            if ord(low[i]) < 0x10ffff:
                high = low[:i] + chr(ord(low[i]) + 1)
                break
        root = self.meta['roots'][INDEX_FIELDS.index(field)]
        for slot in self._prefix_range(root, low, high):
            yield slot, self.read(slot)

    def delete_where(self, field, value, limit=None):
        if limit is not None and limit < 0:
            raise ValueError("Лимит не может быть отрицательным.")

        value = normalize_query(field, value)
        index = INDEX_FIELDS.index(field)
        count = 0

        while limit is None or count < limit:
            candidate = self._lower_bound(index, (value, 0))

            if candidate is None or candidate[0] != value:
                return count

            row = self.read(candidate[1])
            self.delete_key(composite_key(row))
            count += 1

        return count

    def _walk(self, slot):
        if slot:
            node = self._get(slot)
            yield from self._walk(node['l'])
            yield node['key'][1], self.read(node['key'][1])
            yield from self._walk(node['r'])

    def records(self):
        # Обход только живых записей, без сканирования удалённых слотов.
        yield from self._walk(self.meta['roots'][-1])

    def save(self):
        with open(self.path, 'r+b') as file:
            os.fsync(file.fileno())

    @staticmethod
    def _same_path(first, second):
        return (os.path.realpath(first) == os.path.realpath(second)
                or (os.path.exists(first) and os.path.exists(second) and os.path.samefile(first, second)))

    def backup(self, destination):
        if self._same_path(self.path, destination) or self._same_path(self.journal_path, destination):
            raise ValueError('Выберите отдельный файл для резервной копии.')
        self.save()
        self._atomic_copy(self.path, destination)

    @staticmethod
    def _atomic_copy(source, destination):
        fd, staged = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(destination)), suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as out, open(source, 'rb') as inp:
                shutil.copyfileobj(inp, out, 65536)
                out.flush()
                os.fsync(out.fileno())
            os.replace(staged, destination)
        finally:
            if os.path.exists(staged):
                os.unlink(staged)

    def restore(self, source):
        # Проверяем staged, чтобы повреждённый backup не заменил рабочий файл.
        if os.path.exists(os.fspath(source) + '.undo'):
            raise ValueError('Исходный файл имеет незавершённый журнал. Сначала откройте его отдельно для отката.')
        fd, staged = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix='.adb')
        os.close(fd)
        try:
            self._atomic_copy(source, staged)
            restored = FileDatabase(staged)
            restored.check()
            os.replace(staged, self.path)
            self.meta = restored.meta
        finally:
            if os.path.exists(staged):
                os.unlink(staged)

    def clear(self):
        fd, staged = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix='.adb')
        os.close(fd)
        os.unlink(staged)
        try:
            empty = FileDatabase.create(staged)
            os.replace(staged, self.path)
            self.meta = empty.meta
        finally:
            if os.path.exists(staged):
                os.unlink(staged)

    def remove(self):
        os.unlink(self.path)

    @classmethod
    def from_csv(cls, source, destination):
        """Явный потоковый перенос CSV. Исходный файл сохраняется."""
        if os.path.exists(destination):
            raise FileExistsError(destination)
        fd, staged = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(destination)), suffix='.adb')
        os.close(fd)
        os.unlink(staged)
        try:
            db = cls.create(staged)
            with open(source, encoding='utf-8-sig', newline='') as file:
                reader = csv.DictReader(file)
                if reader.fieldnames != FIELDS:
                    raise ValueError('Столбцы CSV не соответствуют схеме.')
                for row in reader:
                    db.add(row)
            # Не затираем внезапно появившийся файл назначения.
            with open(destination, 'xb') as out, open(staged, 'rb') as inp:
                shutil.copyfileobj(inp, out)
            return cls(destination)
        finally:
            if os.path.exists(staged):
                os.unlink(staged)

    def check(self):
        """Диагностика O(n log n), не часть обычного открытия или CRUD.

        Проверяет порядок, высоты, баланс, ссылки на записи, свободные блоки
        и отсутствие потерянных слотов. Не загружает строки в коллекцию.
        """
        def visit(slot, field, low=None, high=None, depth=0):
            if not slot:
                return 0, 0
            if depth > 100:
                raise ValueError('Цикл или слишком глубокий индекс.')
            node = self._get(slot)
            key = tuple(node['key'])
            if node['kind'] != 'node' or (low is not None and key <= low) or (high is not None and key >= high):
                raise ValueError('Нарушен порядок AVL.')
            row = self.read(key[1])
            if row is None or validate(row) != row or self._value(row, field) != key[0]:
                raise ValueError('Неверная ссылка индекса.')
            lh, ln = visit(node['l'], field, low, key, depth + 1)
            rh, rn = visit(node['r'], field, key, high, depth + 1)
            if node['h'] != 1 + max(lh, rh) or abs(lh - rh) > 1:
                raise ValueError('Нарушен баланс AVL.')
            return node['h'], ln + rn + 1
        for i, field in enumerate(INDEX_FIELDS):
            _, count = visit(self.meta['roots'][i], field)
            if count != self.count:
                raise ValueError('Неверный счётчик записей.')
        free_count, slot = 0, self.meta['free']
        while slot:
            node = self._get(slot)
            if node['kind'] != 'free':
                raise ValueError('Неверный список свободных блоков.')
            free_count += 1
            if free_count >= self.meta['slots']:
                raise ValueError('Цикл свободных блоков.')
            slot = node['next']
        row_count = node_count = 0
        for slot in range(1, self.meta['slots']):
            node = self._get(slot)
            if node['kind'] == 'row':
                row_count += 1
                row = self.read(slot)
                if self.find(composite_key(row)) != (slot, row):
                    raise ValueError('Запись отсутствует в первичном индексе.')
            elif node['kind'] == 'node':
                node_count += 1
            elif node['kind'] != 'free':
                raise ValueError('Неизвестный тип блока.')
        if (row_count != self.count or node_count != self.count * len(INDEX_FIELDS)
                or row_count + node_count + free_count != self.meta['slots'] - 1):
            raise ValueError('Потерянные дисковые блоки.')
        return True
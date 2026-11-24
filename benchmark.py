"""Реальные замеры файловой БД, без GUI. python benchmark.py

Время — медиана отдельных операций; генераторы поиска полностью потребляются.
Временные базы автоматически удаляются. Результат печатается как CSV.
"""
import argparse
import csv
import os
import statistics
import sys
import tempfile
import time
from storage import FileDatabase, FIELDS


def flight(number, airline='S7'):
    return dict(zip(FIELDS, [f'SU-{number}', airline, 'DME', 'AER', '02.11.2026',
                            '12:30', 'Airbus', '15000', '150']))


def measure(operation, repeats):
    samples = []  # Только времена, не записи БД.
    for i in range(repeats):
        start = time.perf_counter_ns()
        operation(i)
        samples.append((time.perf_counter_ns() - start) / 1000)
    return round(statistics.median(samples), 2)


def run(size, repeats):
    with tempfile.TemporaryDirectory() as folder:
        db = FileDatabase.create(os.path.join(folder, 'timing.adb'))
        for i in range(size):
            db.add(flight(i, 'RARE' if i < 5 else 'S7'))
        add = measure(lambda i: db.add(flight(size + i, 'ADDED')), repeats)
        hit = measure(lambda i: db.find(f'SU-{i}_02.11.2026'), repeats)
        miss = measure(lambda i: db.find(f'NO-{i}_02.11.2026'), repeats)
        def search(i):
            assert sum(1 for _ in db.search('airline', 'RARE')) == 5
        secondary = measure(search, repeats)
        def update(i):
            db.update(f'SU-{i}_02.11.2026', flight(i, 'EDITED'))
        edit = measure(update, repeats)
        delete = measure(lambda i: db.delete_key(f'SU-{i}_02.11.2026'), repeats)
        db.check()
        return [size, repeats, add, hit, miss, secondary, edit, delete]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sizes', nargs='+', type=int, default=[100, 500, 1000])
    parser.add_argument('--repeats', type=int, default=20)
    args = parser.parse_args()
    if args.repeats <= 0 or any(n < max(5, args.repeats) for n in args.sizes):
        parser.error('sizes >= max(5, repeats), repeats > 0')
    writer = csv.writer(sys.stdout)
    writer.writerow(['n_initial', 'repeats', 'add_us', 'hit_us', 'miss_us',
                     'secondary_5_us', 'edit_us', 'delete_us'])
    for size in args.sizes:
        writer.writerow(run(size, args.repeats))
        sys.stdout.flush()

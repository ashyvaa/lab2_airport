import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
import time
from storage import FileDatabase, FIELDS, composite_key, normalize_query, normalize_prefix, matches, format_value

# --- КОНСТАНТЫ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "airport_db.adb")
OLD_CSV = os.path.join(BASE_DIR, "airport_db.csv")

# Русские названия
FIELD_LABELS = {
    "flight_id": "Номер рейса",
    "airline": "Авиакомпания",
    "departure_airport": "Аэропорт вылета (IATA)",
    "arrival_airport": "Аэропорт прилета (IATA)",
    "departure_date": "Дата вылета (ДД.ММ.ГГГГ)",
    "departure_time": "Время вылета (ЧЧ:ММ)",
    "aircraft_model": "Модель самолета",
    "ticket_price": "Цена билета",
    "seats_count": "Кол-во мест",
    "composite_key": "Полный ключ (Рейс_Дата)"
}

class AirportDatabaseApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Файловая БД: Аэропорт (Составной ключ)")
        self.root.geometry("1200x750")
        
        self.db = None
        self.editing_old_key = None
        self.current_filter = None

        self.create_widgets()
        self.load_data_from_file()

    def create_widgets(self):
        # --- Панель ввода ---
        input_frame = tk.LabelFrame(self.root, text="Добавление / Редактирование записи", padx=10, pady=10)
        input_frame.pack(pady=10, padx=10, fill="x")

        self.entries = {}
        for i, field in enumerate(FIELDS):
            row = i // 4
            col = (i % 4) * 2
            label_text = FIELD_LABELS.get(field, field)
            tk.Label(input_frame, text=label_text + ":").grid(row=row, column=col, sticky="e", padx=5, pady=5)
            entry = tk.Entry(input_frame, width=20)
            entry.grid(row=row, column=col+1, padx=5, pady=5)
            self.entries[field] = entry

        # Блок подсказки
        tk.Label(input_frame, text="* Первичный ключ: Номер рейса + Дата. Для редактирования кликните по строке в таблице.", fg="blue").grid(row=3, column=0, columnspan=8, pady=5)

        # Кнопки
        btn_frame = tk.Frame(input_frame)
        btn_frame.grid(row=4, column=0, columnspan=8, pady=5)
        
        tk.Button(btn_frame, text="Добавить", command=self.add_record, bg="lightgreen", width=15).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Сохранить изменения", command=self.update_record, bg="lightblue", width=18).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Удалить", command=self.delete_record, bg="salmon", width=15).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Очистить поля", command=self.clear_entries, width=15).pack(side="left", padx=5)

        # --- Панель поиска ---
        search_frame = tk.LabelFrame(self.root, text="Поиск", padx=10, pady=10)
        search_frame.pack(pady=5, padx=10, fill="x")

        tk.Label(search_frame, text="Поле поиска:").pack(side="left")
        self.search_field = ttk.Combobox(search_frame, values=list(FIELD_LABELS.values()), state="readonly", width=25)
        self.search_field.set(FIELD_LABELS["composite_key"])
        self.search_field.pack(side="left", padx=5)

        tk.Label(search_frame, text="Значение:").pack(side="left")
        self.search_value = tk.Entry(search_frame, width=20)
        self.search_value.pack(side="left", padx=5)
        
        tk.Button(search_frame, text="Найти", command=self.search_records, width=10).pack(side="left", padx=5)
        tk.Button(search_frame, text="Удалить по значению", command=self.delete_matching_records, width=20).pack(side="left", padx=5)
        tk.Button(search_frame, text="Сбросить", command=self.refresh_tree, width=10).pack(side="left", padx=5)

        search_options = tk.Frame(self.root)
        search_options.pack(fill="x", padx=20)
        self.exact_search = tk.BooleanVar(value=False)
        tk.Checkbutton(search_options, text="Точное совпадение", variable=self.exact_search).pack(side="left")
        tk.Label(search_options, text="Без галочки — поиск по началу значения. Удаление по значению — только точное.").pack(side="left", padx=10)

        # --- Таблица ---
        self.tree = ttk.Treeview(self.root, columns=FIELDS, show="headings", height=15)
        for col in FIELDS:
            self.tree.heading(col, text=FIELD_LABELS.get(col, col))
            self.tree.column(col, width=110, anchor="center")
        self.tree.pack(pady=10, padx=10, fill="both", expand=True)
        
        # Привязка клика по таблице к функции заполнения полей
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        # --- Нижняя панель ---
        bottom_frame = tk.Frame(self.root)
        bottom_frame.pack(pady=10)

        tk.Button(bottom_frame, text="Принудительно сохранить", command=self.save_data_to_file).pack(side="left", padx=10)
        tk.Button(bottom_frame, text="Создать бэкап", command=self.create_backup, bg="lightyellow").pack(side="left", padx=10)
        tk.Button(bottom_frame, text="Восстановить из бэкапа", command=self.restore_backup).pack(side="left", padx=10)
        tk.Button(bottom_frame, text="Экспорт в Excel", command=self.export_to_excel, bg="lightblue").pack(side="left", padx=10)

        menu = tk.Menu(self.root)
        database_menu = tk.Menu(menu, tearoff=False)
        for label, command in (("Новая БД", self.new_database), ("Открыть БД", self.open_database),
                               ("Очистить БД", self.clear_database), ("Удалить БД", self.delete_database)):
            database_menu.add_command(label=label, command=command)
        menu.add_cascade(label="База данных", menu=database_menu)
        self.root.config(menu=menu)

    # --- GUI обращается к FileDatabase, таблица служит только для отображения. ---

    def load_data_from_file(self):
        try:
            if os.path.exists(DB_FILE):
                database = FileDatabase(DB_FILE)
            elif os.path.exists(OLD_CSV) and messagebox.askyesno(
                    "Старая база", "Перенести airport_db.csv в новый формат? Исходный CSV сохранится."):
                database = FileDatabase.from_csv(OLD_CSV, DB_FILE)
            else:
                database = FileDatabase.create(DB_FILE)
            self.use_database(database)
        except Exception as exc:
            messagebox.showerror("Ошибка открытия", str(exc))

    def use_database(self, database):
        self.db = database
        self.clear_entries()
        self.search_value.delete(0, tk.END)
        self.root.title("Файловая БД: " + os.path.basename(database.path))
        self.refresh_tree()

    def require_database(self):
        if self.db is None:
            raise ValueError("Сначала создайте или откройте БД через меню «База данных».")
        return self.db

    def show_rows(self, rows):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for offset, row in rows:
            self.tree.insert("", "end", iid=str(offset), values=[format_value(f, row[f]) for f in FIELDS])

    def refresh_tree(self):
        self.current_filter = None
        try:
            self.show_rows(self.db.records() if self.db else ())
        except Exception as exc:
            messagebox.showerror("Ошибка чтения", str(exc))

    def show_changed_row(self, offset):
        # После добавления/изменения обновляется только одна строка GUI.
        row = self.db.read(offset)
        visible = self.current_filter is None or matches(row, *self.current_filter)
        item = str(offset)
        if self.tree.exists(item):
            if visible:
                self.tree.item(item, values=[format_value(f, row[f]) for f in FIELDS])
            else:
                self.tree.delete(item)
        elif visible:
            self.tree.insert("", "end", iid=item, values=[format_value(f, row[f]) for f in FIELDS])

    def clear_entries(self):
        for entry in self.entries.values():
            entry.delete(0, tk.END)
        self.editing_old_key = None

    def on_tree_select(self, event):
        selected = self.tree.selection()
        if not selected or self.db is None:
            return
        try:
            # Из GUI берем только адрес; значения заново читаем из исходного файла.
            row = self.db.read(int(selected[0]))
            if row is None:
                return
            for field in FIELDS:
                self.entries[field].delete(0, tk.END)
                self.entries[field].insert(0, format_value(field, row[field]))
            self.editing_old_key = composite_key(row)
        except Exception as exc:
            messagebox.showerror("Ошибка чтения", str(exc))

    def form_record(self):
        return {field: entry.get() for field, entry in self.entries.items()}

    @staticmethod
    def print_time(operation, start):
        print(f"[СТАТИСТИКА] {operation}: {(time.perf_counter() - start) * 1000:.4f} мс")

    def add_record(self):
        try:
            database = self.require_database()
            if self.editing_old_key and not messagebox.askyesno(
                    "Добавление", "Выбрана запись. Добавить новую вместо редактирования?"):
                return
            start = time.perf_counter()
            offset = database.add(self.form_record())
            self.print_time("Добавление в файл", start)
            self.show_changed_row(offset)
            self.clear_entries()
            messagebox.showinfo("Успех", "Запись добавлена.")
        except Exception as exc:
            messagebox.showerror("Ошибка добавления", str(exc))

    def update_record(self):
        try:
            database = self.require_database()
            if not self.editing_old_key:
                raise ValueError("Сначала выберите запись в таблице.")
            start = time.perf_counter()
            offset = database.update(self.editing_old_key, self.form_record())
            self.print_time("Редактирование в файле", start)
            self.show_changed_row(offset)
            self.clear_entries()
            messagebox.showinfo("Успех", "Запись изменена.")
        except Exception as exc:
            messagebox.showerror("Ошибка редактирования", str(exc))

    def query(self, exact=False):
        field = next(key for key, label in FIELD_LABELS.items() if label == self.search_field.get())
        value = self.search_value.get().strip()
        if not value:
            raise ValueError("Введите значение в панели поиска.")
        prefix = not (exact or self.exact_search.get())
        normalized = normalize_prefix(field, value) if prefix else normalize_query(field, value)
        return field, normalized, prefix

    def search_records(self):
        try:
            database = self.require_database()
            if not self.search_value.get().strip():
                self.refresh_tree()
                return
            self.current_filter = self.query()
            start = time.perf_counter()
            self.show_rows(database.search(*self.current_filter, limit=1))
            # Генератор читается при выводе: это время поиска вместе с GUI.
            self.print_time("Поиск и отображение", start)
        except Exception as exc:
            messagebox.showerror("Ошибка поиска", str(exc))

    def delete_record(self):
        selected = self.tree.selection()

        if not selected:
            messagebox.showwarning(
                "Удаление",
                "Сначала выберите запись в таблице."
            )
            return

        try:
            offset = int(selected[0])

            row = self.db.read(offset)

            if row is None:
                messagebox.showerror(
                    "Ошибка",
                    "Выбранная запись не найдена в базе данных."
                )
                return

            key = composite_key(row)

            if not self.db.delete_key(key):
                messagebox.showerror(
                    "Ошибка",
                    "Не удалось удалить запись."
                )
                return

            self.tree.delete(selected[0])
            self.clear_entries()

            messagebox.showinfo(
                "Удаление",
                "Запись успешно удалена."
            )

        except Exception as error:
            messagebox.showerror(
                "Ошибка",
                f"Не удалось удалить запись: {error}"
            )

    def delete_matching_records(self):
        try:
            database = self.require_database()
            field, value, _ = self.query(exact=True)
            if not messagebox.askyesno("Удаление", f"Удалить все записи, где «{FIELD_LABELS[field]}» точно равно «{value}»?\nУдаление по началу значения не выполняется."):
                return
            start = time.perf_counter()
            count = database.delete_where(field, value, limit=None)
            self.print_time("Удаление совпадений из файла", start)
            # Перерисовка GUI отделена от времени файловой операции.
            self.clear_entries()
            if self.current_filter is None:
                self.refresh_tree()
            else:
                self.show_rows(database.search(*self.current_filter))
            messagebox.showinfo("Удаление", f"Удалено записей: {count}.")
        except Exception as exc:
            # Групповая операция фиксирует каждую запись отдельно.
            messagebox.showerror("Ошибка удаления", str(exc) +
                                 "\nЧасть совпадений могла быть уже удалена. Обновите таблицу.")

    def save_data_to_file(self):
        try:
            self.require_database().save()
            messagebox.showinfo("Сохранение", "Данные и индекс сохранены.")
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))

    def create_backup(self):
        try:
            database = self.require_database()
            path = filedialog.asksaveasfilename(defaultextension=".adb", initialfile="airport_backup.adb")
            if path:
                database.backup(path)
                messagebox.showinfo("Бэкап", "Резервная копия создана.")
        except Exception as exc:
            messagebox.showerror("Ошибка бэкапа", str(exc))

    def restore_backup(self):
        try:
            database = self.require_database()
            path = filedialog.askopenfilename(filetypes=[("БД / backup", "*.adb"),
                                                        ("Все файлы", "*")])
            if path and messagebox.askyesno("Восстановление", "Заменить текущую БД резервной копией?"):
                database.restore(path)
                self.use_database(database)
                messagebox.showinfo("Успех", "БД восстановлена вместе с индексами.")
        except Exception as exc:
            messagebox.showerror("Ошибка восстановления", str(exc))

    def new_database(self):
        path = filedialog.asksaveasfilename(defaultextension=".adb", filetypes=[("БД аэропорта", "*.adb")])
        if path:
            try:
                self.use_database(FileDatabase.create(path))
            except Exception as exc:
                messagebox.showerror("Ошибка создания", str(exc))

    def open_database(self):
        path = filedialog.askopenfilename(filetypes=[("БД аэропорта", "*.adb")])
        if path:
            try:
                self.use_database(FileDatabase(path))
            except Exception as exc:
                messagebox.showerror("Ошибка открытия", str(exc))

    def clear_database(self):
        try:
            database = self.require_database()
            if messagebox.askyesno("Очистка", "Удалить все записи текущей БД?"):
                database.clear()
                self.use_database(database)
        except Exception as exc:
            messagebox.showerror("Ошибка очистки", str(exc))

    def delete_database(self):
        try:
            database = self.require_database()
            if messagebox.askyesno("Удаление БД", "Удалить файл текущей БД? Это действие необратимо."):
                database.remove()
                self.db = None
                self.clear_entries()
                self.refresh_tree()
                self.root.title("Файловая БД: файл не выбран")
        except Exception as exc:
            messagebox.showerror("Ошибка удаления БД", str(exc))

    def export_to_excel(self):
        try:
            database = self.require_database()
            # Необязательная библиотека больше не мешает запуску основной программы.
            from openpyxl import Workbook
            path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
            if path:
                if database._same_path(database.path, path) or database._same_path(database.journal_path, path):
                    raise ValueError("Экспорт не должен заменять файл БД или журнал.")
                from openpyxl.cell import WriteOnlyCell
                workbook = Workbook(write_only=True)
                sheet = workbook.create_sheet("Рейсы")
                sheet.append(FIELDS)
                for _, row in database.records():
                    cells = []
                    for field in FIELDS:
                        cell = WriteOnlyCell(sheet, value=format_value(field, row[field]))
                        cell.data_type = "s"
                        cells.append(cell)
                    sheet.append(cells)
                workbook.save(path)
                messagebox.showinfo("Успех", "Данные экспортированы в Excel.")
        except ImportError:
            messagebox.showerror("Экспорт", "Для экспорта установите openpyxl: python3 -m pip install openpyxl")
        except Exception as exc:
            messagebox.showerror("Ошибка экспорта", str(exc))


if __name__ == "__main__":
    root = tk.Tk()
    app = AirportDatabaseApp(root)
    root.mainloop()

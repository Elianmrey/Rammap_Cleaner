"""RAMMAP CLEANER - monitoramento inteligente de memória para Windows.

Requisitos:
    py -m pip install psutil

Execute como Administrador. A aplicação solicitará elevação via UAC.

Observações:
- O programa não aumenta a RAM física nem garante ganho de desempenho.
- A limpeza de Working Sets pode causar recarregamento de páginas e page faults.
- A limpeza do cache do sistema utiliza SetSystemFileCacheSize, API documentada
  do Windows, e requer o privilégio SeIncreaseQuotaPrivilege.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Callable, Iterable, Optional, Protocol

import psutil


# ============================================================
# CONFIGURAÇÃO
# ============================================================

MONITOR_INTERVAL_SECONDS = 30
GUI_REFRESH_INTERVAL_MS = 1_000
LOW_MEMORY_THRESHOLD_PERCENT = 10.0
RECOVERY_THRESHOLD_PERCENT = 15.0
LOW_MEMORY_DURATION_SECONDS = 60
MIN_CLEANUP_INTERVAL_SECONDS = 10 * 60
GRAPH_HISTORY_POINTS = 60

PROCESS_SET_QUOTA = 0x0100
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_NOT_ALL_ASSIGNED = 1300

APP_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), ".rammap_cleaner")),
    "RAMMapCleaner",
)
os.makedirs(APP_DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DATA_DIR, "rammap_cleaner.log")


# ============================================================
# LOG
# ============================================================

_log_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    _log_handlers.insert(0, logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("rammap_cleaner")


# ============================================================
# MODELOS
# ============================================================

@dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int
    available_bytes: int
    used_bytes: int
    used_percent: float
    available_percent: float


@dataclass(frozen=True)
class CleanupResult:
    processes_scanned: int
    processes_trimmed: int
    processes_failed: int
    processes_skipped: int
    cache_cleared: bool
    cache_error: str
    duration_seconds: float

    @property
    def has_any_success(self) -> bool:
        return self.processes_trimmed > 0 or self.cache_cleared

    def summary(self) -> str:
        cache_status = "OK" if self.cache_cleared else f"FALHOU ({self.cache_error or 'erro não informado'})"
        return (
            f"Processos: {self.processes_trimmed} liberados, "
            f"{self.processes_failed} falhas, {self.processes_skipped} ignorados; "
            f"cache do sistema: {cache_status}; "
            f"duração: {self.duration_seconds:.1f}s"
        )


def read_memory_snapshot() -> MemorySnapshot:
    """Obtém uma fotografia da memória física usando a métrica disponível do psutil."""
    vm = psutil.virtual_memory()
    total = int(vm.total)
    available = int(vm.available)
    if total <= 0:
        raise RuntimeError("O Windows retornou um total de memória inválido.")
    available = max(0, min(available, total))
    used = total - available
    available_percent = (available / total) * 100.0
    used_percent = (used / total) * 100.0
    return MemorySnapshot(
        total_bytes=total,
        available_bytes=available,
        used_bytes=used,
        used_percent=used_percent,
        available_percent=available_percent,
    )


def format_bytes(value: int) -> str:
    amount = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TB"


# ============================================================
# POLÍTICA DE PRESSÃO DE MEMÓRIA
# ============================================================

class MemoryPressurePolicy:
    """Controla o acionamento automático, com histerese e intervalo mínimo.

    A duração de 60s é acumulada somente enquanto a memória disponível estiver
    abaixo do limite crítico (<10%). Na faixa de histerese (10% a 15%), o
    cronômetro é pausado, não apagado. Acima de 15%, o episódio é reiniciado.

    No máximo uma limpeza automática ocorre por episódio de pressão. Um novo
    episódio só começa depois que a memória disponível supera 15%.
    """

    def __init__(
        self,
        low_threshold_percent: float = LOW_MEMORY_THRESHOLD_PERCENT,
        recovery_threshold_percent: float = RECOVERY_THRESHOLD_PERCENT,
        required_low_duration: float = LOW_MEMORY_DURATION_SECONDS,
        minimum_interval: float = MIN_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        if not 0 <= low_threshold_percent < recovery_threshold_percent <= 100:
            raise ValueError("Os limites de memória precisam ser crescentes e estar entre 0 e 100.")
        if required_low_duration < 0 or minimum_interval < 0:
            raise ValueError("Durações não podem ser negativas.")

        self.low_threshold_percent = low_threshold_percent
        self.recovery_threshold_percent = recovery_threshold_percent
        self.required_low_duration = required_low_duration
        self.minimum_interval = minimum_interval

        self._low_interval_started_at: Optional[float] = None
        self._accumulated_low_seconds = 0.0
        self._episode_triggered = False
        self._last_cleanup_at: Optional[float] = None

    def reset_pressure_tracking(self) -> None:
        """Reinicia o episódio atual sem apagar o histórico de cooldown."""
        self._low_interval_started_at = None
        self._accumulated_low_seconds = 0.0
        self._episode_triggered = False

    def cooldown_remaining(self, now: float) -> float:
        if self._last_cleanup_at is None:
            return 0.0
        elapsed = max(0.0, now - self._last_cleanup_at)
        return max(0.0, self.minimum_interval - elapsed)

    @property
    def episode_triggered(self) -> bool:
        return self._episode_triggered

    def low_duration(self, now: float) -> float:
        active = 0.0
        if self._low_interval_started_at is not None:
            active = max(0.0, now - self._low_interval_started_at)
        return self._accumulated_low_seconds + active

    def evaluate(self, available_percent: float, now: float, enabled: bool = True) -> bool:
        """Retorna True uma única vez quando as condições de limpeza são atendidas."""
        if not enabled:
            self.reset_pressure_tracking()
            return False

        if not 0.0 <= available_percent <= 100.0:
            raise ValueError("available_percent deve estar entre 0 e 100.")

        # Histerese: somente >15% encerra o episódio e limpa o cronômetro.
        if available_percent > self.recovery_threshold_percent:
            self.reset_pressure_tracking()
            return False

        if available_percent < self.low_threshold_percent:
            if self._low_interval_started_at is None:
                self._low_interval_started_at = now
        else:
            # Faixa entre 10% e 15%: pausa a contagem, preservando o acumulado.
            if self._low_interval_started_at is not None:
                self._accumulated_low_seconds += max(0.0, now - self._low_interval_started_at)
                self._low_interval_started_at = None

        if self._episode_triggered:
            return False

        if self.low_duration(now) < self.required_low_duration:
            return False

        if self.cooldown_remaining(now) > 0:
            return False

        self._episode_triggered = True
        self._last_cleanup_at = now
        return True

    def record_manual_cleanup(self, now: float, available_percent: float) -> None:
        """Registra limpeza manual e aplica o cooldown às próximas operações."""
        self._last_cleanup_at = now
        # Se já existe um episódio de pressão, não dispara outra limpeza nele.
        if (
            available_percent < self.low_threshold_percent
            or self._low_interval_started_at is not None
            or self._accumulated_low_seconds > 0
        ):
            self._episode_triggered = True


# ============================================================
# WINDOWS API
# ============================================================

class CleanerApi(Protocol):
    def trim_process(self, pid: int) -> tuple[str, str]: ...
    def clear_system_file_cache(self) -> tuple[bool, str]: ...


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", ctypes.c_uint32)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", ctypes.c_uint32),
        ("Privileges", _LUID_AND_ATTRIBUTES * 1),
    ]


class WindowsApi:
    """Adaptador das APIs documentadas do Windows; carregado apenas no Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("As APIs de memória deste programa estão disponíveis somente no Windows.")

        from ctypes import wintypes

        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE

        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

        self.kernel32.SetProcessWorkingSetSize.argtypes = [
            wintypes.HANDLE,
            ctypes.c_size_t,
            ctypes.c_size_t,
        ]
        self.kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL

        # API documentada para solicitar a liberação do cache de arquivos.
        self.kernel32.SetSystemFileCacheSize.argtypes = [
            ctypes.c_size_t,
            ctypes.c_size_t,
            wintypes.DWORD,
        ]
        self.kernel32.SetSystemFileCacheSize.restype = wintypes.BOOL

        self.kernel32.GetCurrentProcess.argtypes = []
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        self.advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL

        self.advapi32.LookupPrivilegeValueW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(_LUID),
        ]
        self.advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL

        self.advapi32.AdjustTokenPrivileges.argtypes = [
            wintypes.HANDLE,
            wintypes.BOOL,
            ctypes.POINTER(_TOKEN_PRIVILEGES),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    @staticmethod
    def _last_error_text() -> str:
        error_code = ctypes.get_last_error()
        if error_code == 0:
            return "erro do Windows não informado"
        try:
            return f"WinError {error_code}: {ctypes.FormatError(error_code).strip()}"
        except Exception:
            return f"WinError {error_code}"

    def _enable_privilege(self, privilege_name: str) -> tuple[bool, str]:
        token = self.wintypes.HANDLE()
        current_process = self.kernel32.GetCurrentProcess()

        if not self.advapi32.OpenProcessToken(
            current_process,
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token),
        ):
            return False, f"OpenProcessToken: {self._last_error_text()}"

        try:
            luid = _LUID()
            if not self.advapi32.LookupPrivilegeValueW(
                None, privilege_name, ctypes.byref(luid)
            ):
                return False, f"LookupPrivilegeValueW: {self._last_error_text()}"

            privileges = _TOKEN_PRIVILEGES()
            privileges.PrivilegeCount = 1
            privileges.Privileges[0].Luid = luid
            privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED

            ctypes.set_last_error(0)
            if not self.advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(privileges), 0, None, None
            ):
                return False, f"AdjustTokenPrivileges: {self._last_error_text()}"

            error_code = ctypes.get_last_error()
            if error_code == ERROR_NOT_ALL_ASSIGNED:
                return False, (
                    f"O token não possui o privilégio {privilege_name} "
                    f"(ERROR_NOT_ALL_ASSIGNED). Execute como Administrador."
                )
            if error_code != 0:
                return False, f"AdjustTokenPrivileges: WinError {error_code}"
            return True, ""
        finally:
            self.kernel32.CloseHandle(token)

    def trim_process(self, pid: int) -> tuple[str, str]:
        """Retorna ('success'|'skipped'|'failed', mensagem)."""
        # PROCESS_SET_QUOTA é o direito exigido por SetProcessWorkingSetSize.
        access = PROCESS_SET_QUOTA
        handle = self.kernel32.OpenProcess(access, False, pid)
        if not handle:
            error_code = ctypes.get_last_error()
            message = self._last_error_text()
            if error_code in (ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER):
                return "skipped", message
            return "failed", message

        try:
            minus_one = ctypes.c_size_t(-1).value
            if self.kernel32.SetProcessWorkingSetSize(handle, minus_one, minus_one):
                return "success", ""
            error_code = ctypes.get_last_error()
            message = self._last_error_text()
            if error_code in (ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER):
                return "skipped", message
            return "failed", message
        finally:
            self.kernel32.CloseHandle(handle)

    def clear_system_file_cache(self) -> tuple[bool, str]:
        enabled, error = self._enable_privilege("SeIncreaseQuotaPrivilege")
        if not enabled:
            return False, error

        minus_one = ctypes.c_size_t(-1).value
        # Flags=0 mantém o estado atual dos limites; -1 solicita liberar o cache.
        if self.kernel32.SetSystemFileCacheSize(minus_one, minus_one, 0):
            return True, ""
        return False, f"SetSystemFileCacheSize: {self._last_error_text()}"


# ============================================================
# EXECUTOR DE LIMPEZA (INJETÁVEL PARA TESTES)
# ============================================================

class WindowsMemoryCleaner:
    def __init__(
        self,
        api: Optional[CleanerApi] = None,
        process_iter: Optional[Callable[..., Iterable[object]]] = None,
        own_pid: Optional[int] = None,
    ) -> None:
        self.api = api if api is not None else WindowsApi()
        self._process_iter = process_iter if process_iter is not None else psutil.process_iter
        self._own_pid = os.getpid() if own_pid is None else own_pid

    def clean_once(self) -> CleanupResult:
        started = time.monotonic()
        scanned = trimmed = failed = skipped = 0

        try:
            processes = self._process_iter(["pid", "name"])
            for process in processes:
                try:
                    pid = int(process.info["pid"])
                except (AttributeError, KeyError, TypeError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
                    skipped += 1
                    continue

                scanned += 1
                # Evita tocar o próprio monitor e os PIDs reservados do Windows.
                if pid == self._own_pid or pid in (0, 4):
                    skipped += 1
                    continue

                try:
                    outcome, error = self.api.trim_process(pid)
                    if outcome == "success":
                        trimmed += 1
                    elif outcome == "skipped":
                        skipped += 1
                        logger.debug("PID %s ignorado: %s", pid, error)
                    else:
                        failed += 1
                        logger.warning("Falha ao tratar PID %s: %s", pid, error)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    skipped += 1
                except Exception:
                    failed += 1
                    logger.exception("Erro inesperado ao tratar PID %s", pid)
        except Exception:
            logger.exception("Falha ao enumerar processos do Windows")
            failed += 1

        try:
            cache_cleared, cache_error = self.api.clear_system_file_cache()
        except Exception as exc:
            cache_cleared = False
            cache_error = str(exc)
            logger.exception("Erro inesperado ao solicitar limpeza do cache do sistema")

        duration = time.monotonic() - started
        result = CleanupResult(
            processes_scanned=scanned,
            processes_trimmed=trimmed,
            processes_failed=failed,
            processes_skipped=skipped,
            cache_cleared=bool(cache_cleared),
            cache_error=cache_error or "",
            duration_seconds=duration,
        )
        logger.info("Ciclo de limpeza concluído | %s", result.summary())
        return result


# ============================================================
# INTERFACE GRÁFICA
# ============================================================

class RamCleanerApp:
    BG = "#0B1220"
    PANEL = "#121C2E"
    PANEL_ALT = "#18243A"
    TEXT = "#E8EEF7"
    MUTED = "#9AAAC0"
    ACCENT = "#38BDF8"
    GREEN = "#34D399"
    AMBER = "#FBBF24"
    RED = "#FB7185"

    def __init__(self, root: tk.Tk, cleaner: WindowsMemoryCleaner) -> None:
        self.root = root
        self.cleaner = cleaner
        self.policy = MemoryPressurePolicy()
        self.history: deque[float] = deque(maxlen=GRAPH_HISTORY_POINTS)
        self.current_snapshot: Optional[MemorySnapshot] = None
        self.cleaning = False
        self.last_policy_check = time.monotonic() - MONITOR_INTERVAL_SECONDS
        self.auto_enabled = tk.BooleanVar(value=True)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

    def _build_ui(self) -> None:
        self.root.title("RAMMAP CLEANER | Gerenciador Inteligente de Memória")
        self.root.geometry("900x720")
        self.root.minsize(760, 640)
        self.root.configure(bg=self.BG)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "RAM.Horizontal.TProgressbar",
            troughcolor=self.PANEL_ALT,
            background=self.ACCENT,
            bordercolor=self.PANEL_ALT,
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            thickness=13,
        )
        style.configure(
            "RAM.TCheckbutton",
            background=self.PANEL,
            foreground=self.TEXT,
            font=("Segoe UI", 10),
        )
        style.map("RAM.TCheckbutton", background=[("active", self.PANEL)])

        outer = tk.Frame(self.root, bg=self.BG, padx=24, pady=20)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=self.BG)
        header.pack(fill="x", pady=(0, 16))
        tk.Label(
            header,
            text="RAMMAP CLEANER",
            bg=self.BG,
            fg=self.TEXT,
            font=("Segoe UI", 21, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Monitoramento inteligente e liberação sob demanda",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(3, 0))

        # Painel principal de memória.
        memory_card = tk.Frame(outer, bg=self.PANEL, padx=18, pady=16)
        memory_card.pack(fill="x", pady=(0, 14))

        top_line = tk.Frame(memory_card, bg=self.PANEL)
        top_line.pack(fill="x")
        tk.Label(
            top_line,
            text="MEMÓRIA RAM",
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        self.percent_label = tk.Label(
            top_line,
            text="--% em uso",
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Segoe UI", 19, "bold"),
        )
        self.percent_label.pack(side="right")

        self.progress = ttk.Progressbar(
            memory_card,
            style="RAM.Horizontal.TProgressbar",
            maximum=100,
            mode="determinate",
        )
        self.progress.pack(fill="x", pady=(12, 14))

        metrics = tk.Frame(memory_card, bg=self.PANEL)
        metrics.pack(fill="x")
        self.used_label = self._metric(metrics, "EM USO", "--")
        self.available_label = self._metric(metrics, "DISPONÍVEL", "--")
        self.total_label = self._metric(metrics, "TOTAL", "--")

        # Gráfico histórico.
        graph_card = tk.Frame(outer, bg=self.PANEL, padx=16, pady=14)
        graph_card.pack(fill="both", expand=True, pady=(0, 14))
        graph_header = tk.Frame(graph_card, bg=self.PANEL)
        graph_header.pack(fill="x", pady=(0, 8))
        tk.Label(
            graph_header,
            text="HISTÓRICO DE UTILIZAÇÃO",
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        tk.Label(
            graph_header,
            text="Atualização a cada 1 segundo",
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 8),
        ).pack(side="right")

        self.graph = tk.Canvas(
            graph_card,
            height=190,
            bg=self.PANEL,
            highlightthickness=0,
        )
        self.graph.pack(fill="both", expand=True)
        self.graph.bind("<Configure>", lambda _event: self._draw_graph())

        # Controles.
        controls = tk.Frame(outer, bg=self.PANEL, padx=18, pady=15)
        controls.pack(fill="x", pady=(0, 12))

        control_top = tk.Frame(controls, bg=self.PANEL)
        control_top.pack(fill="x")
        self.auto_check = ttk.Checkbutton(
            control_top,
            text="Ativar limpeza automática por pressão de memória",
            variable=self.auto_enabled,
            command=self._on_auto_toggle,
            style="RAM.TCheckbutton",
        )
        self.auto_check.pack(side="left", anchor="w")

        self.clean_button = tk.Button(
            controls,
            text="Liberar Momeoria RAM",
            command=self._manual_cleanup,
            bg="#087EAB",
            activebackground="#0A91C4",
            activeforeground="white",
            fg="white",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            bd=0,
            padx=18,
            pady=11,
            cursor="hand2",
        )
        self.clean_button.pack(fill="x", pady=(13, 8))

        self.status_var = tk.StringVar(value="Inicializando monitoramento...")
        self.status_label = tk.Label(
            controls,
            textvariable=self.status_var,
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=800,
        )
        self.status_label.pack(fill="x")

        footer = tk.Frame(outer, bg=self.BG)
        footer.pack(fill="x")
        tk.Label(
            footer,
            text="Política: abaixo de 10% por 60s • recuperação acima de 15% • intervalo mínimo de 10 min",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w")
        tk.Label(
            footer,
            text="A ferramenta solicita a liberação de páginas; não aumenta a RAM física nem garante ganho de desempenho.",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(3, 0))

    def _metric(self, parent: tk.Widget, title: str, value: str) -> tk.Label:
        frame = tk.Frame(parent, bg=self.PANEL)
        frame.pack(side="left", fill="x", expand=True)
        tk.Label(
            frame,
            text=title,
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w")
        label = tk.Label(
            frame,
            text=value,
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Segoe UI", 12, "bold"),
        )
        label.pack(anchor="w", pady=(3, 0))
        return label

    def _on_auto_toggle(self) -> None:
        if not self.auto_enabled.get():
            self.policy.reset_pressure_tracking()
            self.status_var.set("Limpeza automática desativada. O monitor continua ativo.")
        else:
            self.policy.reset_pressure_tracking()
            self.status_var.set("Limpeza automática ativada. A contagem de pressão começará na próxima verificação.")

    def _poll(self) -> None:
        try:
            snapshot = read_memory_snapshot()
            self.current_snapshot = snapshot
            self.history.append(snapshot.used_percent)
            self._update_memory_widgets(snapshot)

            now = time.monotonic()
            if now - self.last_policy_check >= MONITOR_INTERVAL_SECONDS:
                self.last_policy_check = now
                if not self.auto_enabled.get():
                    self.policy.evaluate(snapshot.available_percent, now, enabled=False)
                elif not self.cleaning:
                    should_clean = self.policy.evaluate(
                        snapshot.available_percent,
                        now,
                        enabled=True,
                    )
                    if should_clean:
                        self.status_var.set(
                            "Pressão persistente de memória detectada. Iniciando um ciclo automático..."
                        )
                        logger.warning(
                            "Acionamento automático: %.2f%% de memória disponível.",
                            snapshot.available_percent,
                        )
                        self._start_cleanup(source="automática")
                    else:
                        self._update_policy_status(snapshot, now)
        except Exception as exc:
            logger.exception("Falha durante o monitoramento da memória")
            self.status_var.set(f"Erro no monitoramento: {exc}")

        self._draw_graph()
        self.root.after(GUI_REFRESH_INTERVAL_MS, self._poll)

    def _update_memory_widgets(self, snapshot: MemorySnapshot) -> None:
        self.percent_label.configure(text=f"{snapshot.used_percent:.1f}% em uso")
        self.progress["value"] = snapshot.used_percent
        self.used_label.configure(text=format_bytes(snapshot.used_bytes))
        self.available_label.configure(
            text=f"{format_bytes(snapshot.available_bytes)} ({snapshot.available_percent:.1f}%)"
        )
        self.total_label.configure(text=format_bytes(snapshot.total_bytes))

        if snapshot.available_percent < LOW_MEMORY_THRESHOLD_PERCENT:
            self.percent_label.configure(fg=self.RED)
        elif snapshot.available_percent <= RECOVERY_THRESHOLD_PERCENT:
            self.percent_label.configure(fg=self.AMBER)
        else:
            self.percent_label.configure(fg=self.TEXT)

    def _update_policy_status(self, snapshot: MemorySnapshot, now: float) -> None:
        available = snapshot.available_percent
        if available < LOW_MEMORY_THRESHOLD_PERCENT:
            elapsed = self.policy.low_duration(now)
            remaining = max(0.0, LOW_MEMORY_DURATION_SECONDS - elapsed)
            if self.policy.episode_triggered:
                self.status_var.set(
                    f"Um ciclo já foi executado neste episódio ({available:.1f}% disponível). "
                    "Aguardando a memória superar 15% para rearmar a limpeza automática."
                )
            elif remaining > 0:
                self.status_var.set(
                    f"Pouca memória disponível ({available:.1f}%). "
                    f"Persistência acumulada: faltam {remaining:.0f}s para avaliar a limpeza automática."
                )
            else:
                cooldown = self.policy.cooldown_remaining(now)
                if cooldown > 0:
                    self.status_var.set(
                        f"Condição crítica confirmada ({available:.1f}% disponível), mas o intervalo de segurança "
                        f"ainda está ativo: {cooldown:.0f}s."
                    )
                else:
                    self.status_var.set(
                        f"Condição crítica confirmada ({available:.1f}% disponível). "
                        "A limpeza será iniciada na próxima avaliação automática."
                    )
        elif available <= RECOVERY_THRESHOLD_PERCENT:
            self.status_var.set(
                f"Faixa de histerese ({available:.1f}% disponível). "
                "A contagem fica pausada até a pressão voltar ou a memória superar 15%."
            )
        else:
            self.status_var.set(
                f"Memória em condição normal: {available:.1f}% disponível. Monitoramento ativo."
            )

    def _draw_graph(self) -> None:
        if not hasattr(self, "graph"):
            return
        canvas = self.graph
        canvas.delete("all")
        width = max(10, canvas.winfo_width())
        height = max(10, canvas.winfo_height())
        left, right, top, bottom = 42, width - 12, 12, height - 24
        plot_width = max(1, right - left)
        plot_height = max(1, bottom - top)

        for level in (0, 25, 50, 75, 100):
            y = bottom - (level / 100.0) * plot_height
            canvas.create_line(left, y, right, y, fill="#26344B", dash=(2, 4))
            canvas.create_text(
                left - 8,
                y,
                text=str(level),
                fill=self.MUTED,
                anchor="e",
                font=("Segoe UI", 7),
            )

        values = list(self.history)
        if len(values) < 2:
            canvas.create_text(
                (left + right) / 2,
                (top + bottom) / 2,
                text="Coletando histórico de memória...",
                fill=self.MUTED,
                font=("Segoe UI", 9),
            )
            return

        coords: list[float] = []
        denominator = max(1, len(values) - 1)
        for index, value in enumerate(values):
            x = left + (index / denominator) * plot_width
            y = bottom - (value / 100.0) * plot_height
            coords.extend((x, y))

        canvas.create_line(*coords, fill=self.ACCENT, width=2, smooth=True)
        canvas.create_text(
            right,
            top,
            text=f"Atual: {values[-1]:.1f}% usada",
            fill=self.TEXT,
            anchor="ne",
            font=("Segoe UI", 8, "bold"),
        )

    def _manual_cleanup(self) -> None:
        if self.cleaning:
            return

        now = time.monotonic()
        remaining = self.policy.cooldown_remaining(now)
        if remaining > 0:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            messagebox.showinfo(
                "Intervalo de segurança",
                "Para evitar limpezas repetidas, aguarde "
                f"{minutes} min {seconds} s antes de executar outro ciclo.",
                parent=self.root,
            )
            return

        if not messagebox.askyesno(
            "Confirmar liberação de memória",
            "Deseja solicitar agora um ciclo de liberação de Working Sets e do cache de arquivos?\n\n"
            "Isso não aumenta a RAM física e pode fazer alguns programas recarregarem páginas de memória.",
            parent=self.root,
        ):
            return

        available = (
            self.current_snapshot.available_percent
            if self.current_snapshot is not None
            else 100.0
        )
        self.policy.record_manual_cleanup(now, available)
        self._start_cleanup(source="manual")

    def _start_cleanup(self, source: str) -> None:
        if self.cleaning:
            return
        self.cleaning = True
        self.clean_button.configure(state="disabled", text="Liberando memória...")
        self.status_var.set(f"Executando ciclo de limpeza {source}. Aguarde...")
        logger.info("Iniciando ciclo de limpeza %s.", source)

        def worker() -> None:
            try:
                result = self.cleaner.clean_once()
                error = None
            except Exception as exc:
                result = None
                error = exc
                logger.exception("Falha inesperada no ciclo de limpeza")

            try:
                self.root.after(0, lambda: self._finish_cleanup(source, result, error))
            except tk.TclError:
                # A janela foi fechada enquanto a thread terminava.
                pass

        threading.Thread(target=worker, name="RamCleanerWorker", daemon=True).start()

    def _finish_cleanup(
        self,
        source: str,
        result: Optional[CleanupResult],
        error: Optional[Exception],
    ) -> None:
        self.cleaning = False
        self.clean_button.configure(state="normal", text="Liberar Momeoria RAM")

        if error is not None:
            self.status_var.set(f"Erro durante a limpeza {source}: {error}")
            messagebox.showerror("Erro na limpeza", str(error), parent=self.root)
            return

        assert result is not None
        logger.info("Resultado da limpeza %s | %s", source, result.summary())
        if result.has_any_success:
            self.status_var.set(f"Ciclo {source} concluído. {result.summary()}")
        else:
            self.status_var.set(
                f"Ciclo {source} finalizado, mas nenhuma operação confirmou sucesso. {result.summary()}"
            )

    def _on_close(self) -> None:
        self.root.destroy()


# ============================================================
# ELEVAÇÃO E ENTRADA
# ============================================================

def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Solicita UAC. Retorna True se uma instância elevada foi iniciada."""
    if os.name != "nt":
        return False

    try:
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p

        if getattr(sys, "frozen", False):
            executable = sys.executable
            parameters = subprocess.list2cmdline(sys.argv[1:])
        else:
            executable = sys.executable
            script_and_args = [os.path.abspath(sys.argv[0]), *sys.argv[1:]]
            parameters = subprocess.list2cmdline(script_and_args)

        result = shell32.ShellExecuteW(
            None,
            "runas",
            executable,
            parameters,
            None,
            1,
        )
        return bool(result and int(result) > 32)
    except Exception:
        logger.exception("Não foi possível solicitar elevação via UAC")
        return False


def main() -> int:
    if os.name != "nt":
        print("ERRO: RAMMAP CLEANER funciona exclusivamente no Windows.")
        return 1

    if not is_admin():
        if relaunch_as_admin():
            return 0
        # Caso o usuário cancele o UAC ou a elevação falhe, explica como prosseguir.
        temp_root = tk.Tk()
        temp_root.withdraw()
        messagebox.showerror(
            "Permissão administrativa necessária",
            "O RAMMAP CLEANER precisa ser executado como Administrador.\n\n"
            "Clique com o botão direito no terminal ou no arquivo e escolha 'Executar como administrador'.",
            parent=temp_root,
        )
        temp_root.destroy()
        return 1

    try:
        cleaner = WindowsMemoryCleaner()
    except Exception as exc:
        logger.exception("Não foi possível inicializar as APIs do Windows")
        temp_root = tk.Tk()
        temp_root.withdraw()
        messagebox.showerror("Falha de inicialização", str(exc), parent=temp_root)
        temp_root.destroy()
        return 1

    root = tk.Tk()
    RamCleanerApp(root, cleaner)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

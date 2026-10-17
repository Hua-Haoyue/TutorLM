"""基于 Docker SDK 的真实运行时适配器。

安全边界（见 docs/architecture.md「数据和权限」）：

* 只操作带 :meth:`RuntimeAdapter.list_managed` 标签的资源，从不枚举或删除
  服务器上其他项目的容器与卷；
* 卷名与容器名一律由配置前缀生成，不接受外部传入的任意名字；
* 每个容器独立卷、独立网络别名，``read_only`` 根文件系统 + ``cap_drop``
  + ``no-new-privileges``，与单实例 compose 保持同一套加固参数。

这个进程因此属于**高权限控制面**，绝不能挂进 instance-app 容器里。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from docker.types import Mount
from docker.errors import ImageNotFound, NotFound as DockerNotFound

from control_plane.domain.models import WorkloadState
from control_plane.errors import RuntimeUnavailable
from control_plane.ports import (
    InstanceSpec,
    ReadinessResult,
    RuntimeStatus,
    VolumeCopySpec,
    VolumeSpec,
)

logger = logging.getLogger(__name__)

#: 就绪探针路径，与 contracts/instance-runtime-v1.md 冻结的约定一致。
READY_PATH = "/health/ready"

#: 卷复制 helper 的固定挂载点：源只读、目标可写。
COPY_SOURCE_MOUNT = '/copy/source'
COPY_TARGET_MOUNT = '/copy/target'
#: 副本宿主 uid/gid：与 instance-app 容器内 app 用户一致，保证接收方可写。
COPY_OWNER_UID = 10001
#: 等 helper 退出的额外宽限（helper 自身也有 deadline，先于该宽限退出）。
COPY_WAIT_GRACE_SECONDS = 10

#: 复制脚本：只用 Python 标准库，拒绝符号链接与特殊文件，把目标交给 app uid。
_COPY_SCRIPT = r"""
import json
import os
import stat
import sys
import time

SRC_ROOT = "/copy/source"
DST_ROOT = "/copy/target"
UID = 10001
GID = 10001


def die(code, message):
    sys.stderr.write("volume-copy helper: " + message + "\n")
    raise SystemExit(code)


def clean(value, label):
    text = (value or "").strip()
    if text in ("", "."):
        return ""
    if text.startswith("/") or text.startswith("\\") or ":" in text:
        die(2, label + " must be a strictly relative path: " + repr(value))
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        return ""
    if any(part == ".." for part in parts):
        die(2, label + " must not contain '..': " + repr(value))
    return "/".join(parts)


def join(root, rel):
    return root if not rel else os.path.join(root, *rel.split("/"))


def fix(path, is_dir):
    # chmod 必须在 chown 之前：helper 只有 CAP_CHOWN，没有 CAP_FOWNER。
    # 一律不跟随符号链接：先 lstat 确认不是链接，再用 lchown（等价 chown -h），
    # 这样即使发生竞态也不会把权限/所有权经链接交给别处。
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            die(3, "refusing to fix ownership through a symlink: " + path)
        os.chmod(path, 0o755 if is_dir else 0o644)
        os.lchown(path, UID, GID)
    except OSError as exc:
        die(8, "cannot hand " + path + " to uid " + str(UID) + ": " + str(exc))


def copy_file(source, target, deadline):
    try:
        with open(source, "rb") as reader:
            with open(target, "wb") as writer:
                while True:
                    if time.monotonic() > deadline:
                        die(7, "copy exceeded its deadline")
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
    except PermissionError as exc:
        die(8, "cannot read " + source + " as uid 0 without DAC_OVERRIDE; project "
               "files must stay world-readable: " + str(exc))
    except OSError as exc:
        die(6, "cannot copy " + source + ": " + str(exc))
    fix(target, False)


def main():
    if len(sys.argv) < 7:
        die(2, "helper arguments are incomplete")
    source_rel = clean(sys.argv[1], "source_directory")
    target_rel = clean(sys.argv[2], "destination_directory")
    try:
        deadline_seconds = float(sys.argv[5])
    except ValueError:
        die(2, "helper deadline is invalid")
    deadline = time.monotonic() + max(1.0, deadline_seconds)
    excludes = set()
    excluded_names = set(json.loads(sys.argv[6]))
    for raw in sys.argv[7:]:
        value = clean(raw, "exclude_paths")
        if value:
            excludes.add(value)

    source_root = join(SRC_ROOT, source_rel)
    target_root = join(DST_ROOT, target_rel)

    if os.path.islink(source_root):
        die(3, "source is a symlink: " + source_root)
    if not os.path.isdir(source_root):
        die(2, "source directory is missing: " + source_root)

    # 只创建目标目录链，先不 chown：目录仍属 root，helper 才能在里层继续写入。
    chain = [DST_ROOT]
    current = DST_ROOT
    if target_rel:
        for part in target_rel.split("/"):
            current = os.path.join(current, part)
            try:
                os.makedirs(current, exist_ok=True)
            except OSError as exc:
                die(6, "cannot create " + current + ": " + str(exc))
            chain.append(current)

    # 显式栈做后序遍历：子项复制完再 chown 目录，避免目录先归 10001 后
    # uid 0 失去写权限。
    stack = [(source_root, target_root, "", False)]
    while stack:
        if time.monotonic() > deadline:
            die(7, "copy exceeded its deadline")
        source_dir, target_dir, prefix, processed = stack.pop()
        if processed:
            fix(target_dir, True)
            continue
        try:
            entries = list(os.scandir(source_dir))
        except OSError as exc:
            die(6, "cannot list " + source_dir + ": " + str(exc))
        stack.append((source_dir, target_dir, prefix, True))
        for entry in entries:
            if entry.name in excluded_names:
                continue
            rel = entry.name if not prefix else prefix + "/" + entry.name
            if any(rel == item or rel.startswith(item + "/") for item in excludes):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                die(6, "cannot stat " + entry.path + ": " + str(exc))
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                die(3, "refusing symlink: " + entry.path)
            if stat.S_ISDIR(mode):
                target_sub = os.path.join(target_dir, entry.name)
                try:
                    os.makedirs(target_sub, exist_ok=True)
                except OSError as exc:
                    die(6, "cannot create " + target_sub + ": " + str(exc))
                stack.append((entry.path, target_sub, rel, False))
            elif stat.S_ISREG(mode):
                copy_file(entry.path, os.path.join(target_dir, entry.name), deadline)
            else:
                die(3, "refusing special file: " + entry.path)

    # target_root was already finalized by the post-order walk. Repeating chmod
    # after chown fails under CAP_CHOWN-only permissions (no CAP_FOWNER).
    for path in reversed(chain[:-1]):
        fix(path, True)
    sys.stdout.write("copied\n")
    return 0


raise SystemExit(main())
"""


def _map_state(attrs: dict[str, Any]) -> WorkloadState:
    state = (attrs.get("State") or {})
    status = str(state.get("Status") or "").lower()
    if status == "running":
        health = str((state.get("Health") or {}).get("Status") or "").lower()
        if health in {"unhealthy", "starting"}:
            return WorkloadState.STARTING
        return WorkloadState.READY
    if status in {"created"}:
        return WorkloadState.CREATED
    if status in {"exited", "dead"}:
        return WorkloadState.STOPPED
    if status in {"paused", "restarting", "removing"}:
        return WorkloadState.STOPPING
    return WorkloadState.STOPPED


class DockerRuntimeAdapter:
    """把控制面的容器操作翻译成 Docker API。"""

    name = "docker"

    def __init__(self, config: Any, *, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            try:
                import docker  # 延迟导入，方便无 Docker 环境下做静态检查
            except ImportError as exc:  # pragma: no cover
                raise RuntimeUnavailable("未安装 docker SDK") from exc
            try:
                self._client = docker.DockerClient(
                    base_url=config.docker_host, timeout=60
                )
                self._client.ping()
            except Exception as exc:
                raise RuntimeUnavailable(f"无法连接 Docker daemon：{exc}") from exc
        self._config = config
        self._owned_labels = config.labels()

    # ------------------------------------------------------------ 内部
    def _guard_owned(self, container: Any) -> None:
        """确认容器属于本方，拒绝触碰其他项目的资源。"""

        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        for key, value in self._owned_labels.items():
            if labels.get(key) != value:
                raise RuntimeUnavailable(
                    f"拒绝操作非本方容器 {container.name}（缺少归属标签）"
                )

    # ------------------------------------------------------------ volumes
    def volume_exists(self, name: str) -> bool:
        return self._lookup_volume(name) is not None

    def ensure_volume(self, spec: VolumeSpec) -> bool:
        try:
            volume = self._client.volumes.get(spec.name)
        except (DockerNotFound, KeyError):
            volume = None
        except Exception as exc:
            raise RuntimeUnavailable(f'Cannot inspect volume {spec.name}: {exc}') from exc
        if volume is not None:
            labels = (volume.attrs or {}).get('Labels') or {}
            if any(labels.get(key) != value for key, value in spec.labels.items()):
                raise RuntimeUnavailable('Refusing to mount a volume with mismatched ownership labels')
            return False
        if spec.must_exist:
            raise RuntimeUnavailable('Persistent workspace volume is missing; refusing an empty replacement')
        try:
            volume = self._client.volumes.create(name=spec.name, labels=spec.labels)
            labels = (volume.attrs or {}).get('Labels') or {}
            if any(labels.get(key) != value for key, value in spec.labels.items()):
                raise RuntimeUnavailable('Created volume has mismatched ownership labels')
            return True
        except Exception as exc:
            raise RuntimeUnavailable(f"创建数据卷失败 {spec.name}：{exc}") from exc

    def remove_volume(self, name: str) -> bool:
        try:
            volume = self._client.volumes.get(name)
        except (DockerNotFound, KeyError):
            return False
        except Exception as exc:
            raise RuntimeUnavailable(f'Cannot inspect volume {name}: {exc}') from exc
        labels = (volume.attrs or {}).get("Labels") or {}
        for key, value in self._owned_labels.items():
            if labels.get(key) != value:
                raise RuntimeUnavailable(f"拒绝删除非本方数据卷 {name}")
        try:
            volume.remove(force=False)
            return True
        except Exception as exc:
            raise RuntimeUnavailable(f"删除数据卷失败 {name}：{exc}") from exc


    # ------------------------------------------------------------ 卷复制
    def _lookup_volume(self, name: str) -> Any | None:
        try:
            return self._client.volumes.get(name)
        except (DockerNotFound, KeyError):
            return None
        except Exception as exc:
            raise RuntimeUnavailable(f'无法检查数据卷 {name}：{exc}') from exc

    def _require_owned_volume(self, spec: VolumeSpec) -> Any:
        """确认源卷存在且归属标签完全匹配，否则拒绝读取。"""

        volume = self._lookup_volume(spec.name)
        if volume is None:
            raise RuntimeUnavailable(f'源卷 {spec.name} 不存在，拒绝复制')
        labels = (getattr(volume, 'attrs', None) or {}).get('Labels') or {}
        if any(labels.get(key) != value for key, value in self._owned_labels.items()):
            raise RuntimeUnavailable(f'拒绝读取非本方数据卷 {spec.name}')
        if any(labels.get(key) != value for key, value in spec.labels.items()):
            raise RuntimeUnavailable(f'源卷 {spec.name} 归属标签不匹配')
        return volume

    def _refuse_online_source(self, volume_name: str) -> None:
        """拒绝复制仍被运行中容器写入的源卷，避免静默做在线快照。

        用 **volume 过滤器**精确列出挂载该源卷的容器，而不是按本方标签枚举：
        非本方的写入者（其他项目的容器）挂在同一个卷上时也必须被发现并拒绝，
        否则会把并发写当成一致快照。这里只读不操作，也不枚举与该卷无关的
        其他项目资源。后台服务会先停止源容器；这里是二次确认，单独误调用时
        会直接失败。
        """

        try:
            containers = self._client.containers.list(
                all=True, filters={'volume': volume_name}
            )
        except Exception as exc:
            raise RuntimeUnavailable(
                f'无法枚举挂载源卷 {volume_name} 的容器：{exc}'
            ) from exc
        for container in containers:
            attrs = getattr(container, 'attrs', None) or {}
            state = str((attrs.get('State') or {}).get('Status') or '').lower()
            if state != 'running':
                continue
            for mount in attrs.get('Mounts') or []:
                if mount.get('Type') == 'volume' and mount.get('Name') == volume_name:
                    raise RuntimeUnavailable(
                        f'源卷 {volume_name} 仍被运行中的容器 {container.name} 挂载；'
                        '必须先停止写入者再复制，避免在线快照'
                    )

    def _require_local_image(self, image: str) -> None:
        """helper 镜像必须已在本地；缺失直接报错，绝不隐式拉取。"""

        if not image:
            raise RuntimeUnavailable('未配置卷复制 helper 镜像')
        try:
            self._client.images.get(image)
        except (ImageNotFound, DockerNotFound) as exc:
            raise RuntimeUnavailable(
                f'本机缺少卷复制 helper 镜像 {image}；拒绝隐式拉取，请先手工准备该镜像'
            ) from exc
        except Exception as exc:
            raise RuntimeUnavailable(f'检查卷复制 helper 镜像 {image} 失败：{exc}') from exc

    def _create_copy_helper(
        self, spec: VolumeCopySpec, image: str, helper_name: str, labels: dict[str, str]
    ) -> Any:
        command = [
            'python3',
            '-c',
            _COPY_SCRIPT,
            spec.source_directory,
            spec.destination_directory,
            str(COPY_OWNER_UID),
            str(COPY_OWNER_UID),
            str(int(spec.timeout_seconds)),
            json.dumps(spec.exclude_names),
            *spec.exclude_paths,
        ]
        docker_spec: dict[str, Any] = {
            'image': image,
            'name': helper_name,
            'entrypoint': command,
            'detach': True,
            'labels': labels,
            # root + 仅回补 CHOWN：新建卷根目录属 root，必须先交出所有权。
            'user': '0:0',
            'network_disabled': True,
            'mounts': [
                Mount(
                    target=COPY_SOURCE_MOUNT,
                    source=spec.source.name,
                    type='volume',
                    read_only=True,
                ),
                Mount(
                    target=COPY_TARGET_MOUNT,
                    source=spec.destination.name,
                    type='volume',
                    read_only=False,
                ),
            ],
            'read_only': True,
            'tmpfs': {'/tmp': f'size={self._config.tmpfs_size or "64m"},mode=1777'},
            'cap_drop': ['ALL'],
            'cap_add': ['CHOWN'],
            'security_opt': ['no-new-privileges:true'],
            'init': True,
        }
        if self._config.mem_limit:
            docker_spec['mem_limit'] = self._config.mem_limit
        if self._config.cpus:
            docker_spec['nano_cpus'] = int(float(self._config.cpus) * 1_000_000_000)
        if self._config.pids_limit:
            docker_spec['pids_limit'] = int(self._config.pids_limit)
        try:
            return self._client.containers.create(**docker_spec)
        except Exception as exc:
            raise RuntimeUnavailable(f'创建卷复制 helper {helper_name} 失败：{exc}') from exc

    def _wait_for_helper(self, helper: Any, timeout_seconds: int) -> int:
        """有界等待 helper 退出；超时/中断就强杀，绝不无限阻塞。"""

        try:
            result = helper.wait(timeout=timeout_seconds + COPY_WAIT_GRACE_SECONDS)
        except Exception as exc:
            self._kill_helper(helper)
            raise RuntimeUnavailable(f'卷复制 helper 超时或被中断：{exc}') from exc
        status = result.get('StatusCode') if isinstance(result, dict) else result
        try:
            return int(status)
        except (TypeError, ValueError):
            raise RuntimeUnavailable(f'无法解析卷复制 helper 退出状态：{result!r}') from None

    def _kill_helper(self, helper: Any) -> None:
        try:
            helper.kill()
        except Exception:
            logger.warning('强制终止卷复制 helper 失败', exc_info=True)

    def _helper_logs(self, helper: Any) -> str:
        try:
            raw = helper.logs(stdout=True, stderr=True)
        except Exception:
            return '<日志不可用>'
        text = raw.decode('utf-8', 'replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
        return text.strip()[-1000:] or '<空>'

    def _stop_and_remove_helper(self, helper: Any | None, helper_name: str) -> str | None:
        """停止并删除 helper，返回清理失败说明（``None`` 表示清理干净）。

        残留的 helper 会一直挂载源卷与目标卷，让项目卷 GC 失效，所以清理失败
        必须向上暴露，不能吞掉。
        """

        if helper is None:
            return None
        problems: list[str] = []
        try:
            helper.stop(timeout=10)
        except Exception as exc:
            problems.append(f'停止 helper {helper_name} 失败：{exc}')
            try:
                helper.kill()
            except Exception as kill_exc:
                problems.append(f'强制终止 helper {helper_name} 失败：{kill_exc}')
        try:
            helper.remove(force=True, v=False)
        except Exception as exc:
            problems.append(f'删除 helper {helper_name} 失败：{exc}')
        return '；'.join(problems) or None

    def _rollback_volume(self, name: str, labels: dict[str, str]) -> str | None:
        """尽力删除本次新建的目标卷；返回失败说明，``None`` 表示已清理或不存在。"""

        try:
            volume = self._client.volumes.get(name)
        except (DockerNotFound, KeyError):
            return None
        except Exception as exc:
            return f'检查目标卷 {name} 失败：{exc}'
        actual = (getattr(volume, 'attrs', None) or {}).get('Labels') or {}
        if any(actual.get(key) != value for key, value in (labels or {}).items()):
            return f'目标卷 {name} 归属标签不匹配，拒绝自动回滚删除'
        try:
            volume.remove(force=False)
        except Exception as exc:
            return f'删除目标卷 {name} 失败：{exc}'
        return None

    def _copy_failure_details(
        self,
        spec: VolumeCopySpec,
        *,
        cleanup_error: str | None,
        rollback_error: str | None,
        copy_completed: bool,
    ) -> dict[str, Any]:
        """把复制失败后的**残留状态**做成机器可读的 details 交给上游。

        上游（后台 service）据此判断是否需要人工清理：``helper_cleaned`` 为
        False 说明有残留 helper 仍挂着卷；``destination_rolled_back`` 为 False
        说明目标卷可能已存在且含不完整数据，重试前必须先处理，不能再当全新卷。
        """

        return {
            'operation': 'copy_volume',
            'source_volume': spec.source.name,
            'destination_volume': spec.destination.name,
            'copy_completed': bool(copy_completed),
            'helper_cleaned': cleanup_error is None,
            'destination_rolled_back': rollback_error is None,
            'destination_may_contain_partial_data': rollback_error is not None,
            'cleanup_error': cleanup_error,
            'rollback_error': rollback_error,
        }

    def copy_volume(self, spec: VolumeCopySpec) -> None:
        """把一个卷中的目录子树复制到一个全新卷。

        * 源卷必须存在且归属标签完全匹配；目标卷**必须不存在**，绝不覆盖；
        * helper 镜像必须已在本地，缺失直接报错，绝不拉取；
        * helper 独立命名 + 本方标签，源卷只读、目标卷可写，禁网、
          ``cap_drop=ALL`` 仅回补 ``CHOWN``、``no-new-privileges``，并有
          内存/CPU/进程与时间上限；
        * 只用 Python 标准库复制，拒绝符号链接与特殊文件，并把目标交给
          app uid 10001，保证接收方实例可写；
        * 复制前用 volume 过滤器确认没有容器（含非本方）仍在写源卷；
        * 无论成功失败都停止并删除 helper；失败时尽力回滚目标卷并暴露失败。
        """

        self._require_owned_volume(spec.source)
        if self._lookup_volume(spec.destination.name) is not None:
            raise RuntimeUnavailable(
                f'拒绝覆盖已存在的目标卷 {spec.destination.name}；卷复制只允许写全新卷'
            )
        self._refuse_online_source(spec.source.name)

        image = (spec.helper_image or '').strip() or self._config.instance_image
        self._require_local_image(image)

        if not self.ensure_volume(spec.destination):
            raise RuntimeUnavailable(f'目标卷 {spec.destination.name} 已存在，拒绝继续复制')

        helper_name = f'{self._config.container_prefix}-copy-{uuid.uuid4().hex[:12]}'
        labels = dict(self._owned_labels)
        labels['tutorlm.copy'] = 'volume'
        helper: Any | None = None
        copied = False
        try:
            helper = self._create_copy_helper(spec, image, helper_name, labels)
            helper.start()
            exit_code = self._wait_for_helper(helper, int(spec.timeout_seconds))
            if exit_code != 0:
                raise RuntimeUnavailable(
                    f'卷复制 helper 退出码 {exit_code}；日志：{self._helper_logs(helper)}'
                )
            copied = True
        except BaseException as exc:
            cleanup_error = self._stop_and_remove_helper(helper, helper_name)
            rollback_error = (
                None if copied
                else self._rollback_volume(spec.destination.name, spec.destination.labels)
            )
            if cleanup_error or rollback_error:
                detail = '；'.join(
                    item for item in (
                        f'helper 清理失败：{cleanup_error}' if cleanup_error else None,
                        f'目标卷回滚失败：{rollback_error}' if rollback_error else None,
                    ) if item
                )
                raise RuntimeUnavailable(
                    f'卷复制失败（{exc}），且清理未完成：{detail}',
                    details=self._copy_failure_details(
                        spec,
                        cleanup_error=cleanup_error,
                        rollback_error=rollback_error,
                        copy_completed=copied,
                    ),
                ) from exc
            raise

        cleanup_error = self._stop_and_remove_helper(helper, helper_name)
        if cleanup_error:
            raise RuntimeUnavailable(
                f'卷复制已完成，但 helper 清理失败（残留挂载会让卷 GC 失效）：{cleanup_error}',
                details=self._copy_failure_details(
                    spec, cleanup_error=cleanup_error, rollback_error=None,
                    copy_completed=True,
                ),
            )
        return None

    # ------------------------------------------------------------ 生命周期
    def _initialize_workspace_directory(self, spec: InstanceSpec) -> None:
        """Hand only a new workspace data directory to the non-root app user."""
        from pathlib import PurePosixPath
        data = PurePosixPath(spec.env['TUTORLM_DATA_DIR'])
        try:
            relative = data.relative_to(PurePosixPath(spec.mount_path))
        except ValueError:
            raise RuntimeUnavailable('Workspace data directory is outside its volume') from None
        if not relative.parts or '..' in relative.parts:
            raise RuntimeUnavailable('Workspace initialization requires a data subdirectory')
        self._require_local_image(spec.image)
        script = '''import os, pathlib, stat, sys
root = pathlib.Path('/workspace-init')
parts = pathlib.PurePosixPath(sys.argv[1]).parts
current = root
for part in parts:
    current = current / part
    try:
        current.mkdir(mode=0o755)
    except FileExistsError:
        pass
    info = current.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, 10001):
        raise RuntimeError('Unsafe workspace directory')
info = current.lstat()
if info.st_uid == 0:
    if any(current.iterdir()):
        raise RuntimeError('Refusing to change ownership of existing project data')
    os.chmod(current, 0o755)
    os.chown(current, 10001, 10001)
'''
        helper = None
        name = f'{spec.container_name}-init-{uuid.uuid4().hex[:8]}'
        try:
            helper = self._client.containers.create(
                image=spec.image, name=name, entrypoint=['python', '-c', script, str(relative)],
                user='0:0', detach=True, network_mode='none', read_only=True,
                mounts=[Mount(target='/workspace-init', source=spec.volume_name, type='volume')],
                cap_drop=['ALL'], cap_add=['CHOWN'], security_opt=['no-new-privileges:true'],
                mem_limit='128m', pids_limit=32, labels={**spec.labels, 'tutorlm.helper': 'workspace-init'},
            )
            helper.start()
            if self._wait_for_helper(helper, 30) != 0:
                raise RuntimeUnavailable('Workspace directory initialization failed')
        finally:
            if self._stop_and_remove_helper(helper, name):
                raise RuntimeUnavailable('Workspace initialization helper cleanup failed')

    def create(self, spec: InstanceSpec) -> RuntimeStatus:
        self.ensure_volume(VolumeSpec(name=spec.volume_name, labels=spec.labels, must_exist=spec.volume_must_exist))
        try:
            existing = self._client.containers.get(spec.container_name)
            self._guard_owned(existing)
            return self.status(spec.container_name)
        except RuntimeUnavailable:
            raise
        except Exception:
            pass

        if spec.env.get('TUTORLM_PERSISTENT_WORKSPACE') == 'true' and spec.env.get('TUTORLM_WORKSPACE_SUBDIRECTORY') != '.':
            self._initialize_workspace_directory(spec)

        docker_spec: dict[str, Any] = {
            "image": spec.image,
            "name": spec.container_name,
            "detach": True,
            "environment": dict(spec.env),
            "labels": dict(spec.labels),
            # 用显式 Mount 而不是 volumes 字典：Mount 的 source/target 语义
            # 无歧义，且保证挂的是**具名卷**（不会退化成匿名卷留下垃圾数据）。
            "mounts": [
                Mount(
                    target=spec.mount_path,
                    source=spec.volume_name,
                    type="volume",
                    read_only=False,
                )
            ],
            "read_only": True,
            "tmpfs": {"/tmp": f"size={spec.tmpfs_size or '128m'},mode=1777"},
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "init": True,
        }
        if spec.mem_limit:
            docker_spec["mem_limit"] = spec.mem_limit
        if spec.cpus:
            docker_spec["nano_cpus"] = int(float(spec.cpus) * 1_000_000_000)
        if spec.pids_limit:
            docker_spec["pids_limit"] = int(spec.pids_limit)
        if spec.network:
            docker_spec["network"] = spec.network
        try:
            container = self._client.containers.create(**docker_spec)
            if spec.network and hasattr(self._client, "networks"):
                # Keep the gateway target stable while generation-specific
                # container names change on every reset.
                network = self._client.networks.get(spec.network)
                # ``containers.create(network=...)`` attaches the container
                # without aliases. Reconnect before first start so the stable
                # slot alias is actually registered in Docker DNS.
                network.disconnect(container, force=True)
                network.connect(
                    container,
                    aliases=[f"{self._config.container_prefix}-s{spec.slot_id:02d}"],
                )
        except Exception as exc:
            raise RuntimeUnavailable(f"创建容器失败 {spec.container_name}：{exc}") from exc
        return self.status(spec.container_name)

    def start(self, container_name: str) -> RuntimeStatus:
        try:
            container = self._client.containers.get(container_name)
            self._guard_owned(container)
            container.start()
        except RuntimeUnavailable:
            raise
        except Exception as exc:
            raise RuntimeUnavailable(f"启动容器失败 {container_name}：{exc}") from exc
        return self.status(container_name)

    def stop(self, container_name: str, *, timeout: int) -> RuntimeStatus:
        try:
            container = self._client.containers.get(container_name)
        except Exception:
            return RuntimeStatus(exists=False, state=WorkloadState.ABSENT)
        self._guard_owned(container)
        try:
            container.stop(timeout=timeout)
        except Exception as exc:
            raise RuntimeUnavailable(f"停止容器失败 {container_name}：{exc}") from exc
        return self.status(container_name)

    def remove(self, container_name: str, *, remove_volumes: bool = False) -> bool:
        try:
            container = self._client.containers.get(container_name)
        except Exception:
            return False
        self._guard_owned(container)
        try:
            # 具名卷不会被 v=True 删除，回收由 remove_volume 显式负责。
            container.remove(force=True)
            return True
        except Exception as exc:
            raise RuntimeUnavailable(f"删除容器失败 {container_name}：{exc}") from exc

    def status(self, container_name: str) -> RuntimeStatus:
        try:
            container = self._client.containers.get(container_name)
        except Exception:
            return RuntimeStatus(exists=False, state=WorkloadState.ABSENT)
        attrs = container.attrs
        state = _map_state(attrs)
        labels = (attrs.get("Config") or {}).get("Labels") or {}
        mounts = attrs.get("Mounts") or []
        volume_exists = any(m.get("Type") == "volume" for m in mounts)
        return RuntimeStatus(
            exists=True,
            state=state,
            container_name=container_name,
            volume_exists=volume_exists,
            upstream=self._upstream(container_name),
            running=state in {WorkloadState.READY, WorkloadState.STARTING},
        )

    def _upstream(self, container_name: str) -> str:
        """容器内网地址。

        应用容器在同一个 Docker 网络里按容器名解析；端口用内部端口，
        绝不经过宿主机发布端口（避免绕过网关）。
        """

        return f"http://{container_name}:{self._config.instance_port}"

    # ------------------------------------------------------------ 就绪
    def probe(self, upstream: str) -> ReadinessResult:
        import httpx

        url = f"{upstream.rstrip('/')}{READY_PATH}"
        try:
            response = httpx.get(url, timeout=3.0)
        except Exception as exc:
            return ReadinessResult(ready=False, detail=f"探针请求失败：{exc}")
        if response.status_code != 200:
            return ReadinessResult(
                ready=False, detail=f"探针返回 {response.status_code}"
            )
        try:
            payload = response.json()
        except Exception:
            return ReadinessResult(ready=False, detail="探针响应不是 JSON")
        if str(payload.get("status")) != "ok":
            return ReadinessResult(ready=False, detail=f"status={payload.get('status')!r}")
        return ReadinessResult(ready=True, payload=payload)

    def inspect_labels(self, container_name: str) -> dict[str, str]:
        try:
            container = self._client.containers.get(container_name)
        except Exception as exc:
            raise RuntimeUnavailable(f"容器不存在：{container_name}") from exc
        return dict((container.attrs.get("Config") or {}).get("Labels") or {})

    def list_managed(self) -> list[RuntimeStatus]:
        try:
            containers = self._client.containers.list(
                all=True, filters={"label": f"{self._config.label_key}={self._config.label_value}"}
            )
        except Exception as exc:
            raise RuntimeUnavailable(f"枚举容器失败：{exc}") from exc
        return [self.status(c.name) for c in containers]

    def wait_ready(
        self, upstream: str, *, timeout: float, interval: float
    ) -> ReadinessResult:
        """轮询直到就绪或超时。返回最后一次探测结果。"""

        deadline = time.monotonic() + timeout
        last = ReadinessResult(ready=False, detail="未开始探测")
        while True:
            last = self.probe(upstream)
            if last.ready:
                return last
            if time.monotonic() >= deadline:
                return last
            time.sleep(interval)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover
            logger.debug("关闭 Docker 客户端失败", exc_info=True)

import 'package:flutter/material.dart';
import '../scope/firmware_update.dart';

class FirmwareUpdateDialog extends StatefulWidget {
  final FirmwareUpdater updater;
  const FirmwareUpdateDialog({
    super.key,
    this.updater = const FirmwareUpdater(),
  });

  @override
  State<FirmwareUpdateDialog> createState() => _FirmwareUpdateDialogState();
}

class _FirmwareUpdateDialogState extends State<FirmwareUpdateDialog> {
  FirmwareFile? _file;
  IapDevice? _device;
  bool _working = false, _writing = false;
  int _sent = 0, _total = 0;
  String? _status, _error;

  Future<void> _run(Future<void> Function() action) async {
    if (_working) return;
    setState(() {
      _working = true;
      _error = null;
    });
    try {
      await action();
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = localUpdaterError(error.toString());
          if (_writing) {
            _status =
                'Запись прервана. Войдите в IAP заново, прежде чем повторять обновление.';
          } else {
            _status = null;
          }
        });
      }
    } finally {
      if (mounted) setState(() => _working = _writing = false);
    }
  }

  Future<void> _select() => _run(() async {
    final FirmwareFile? file;
    try {
      file = await widget.updater.select();
    } catch (_) {
      if (mounted) setState(() => _file = null);
      rethrow;
    }
    if (file != null && mounted) {
      setState(() {
        _file = file;
        _status = null;
      });
    }
  });

  Future<void> _probe() => _run(() async {
    setState(() {
      _device = null;
      _status =
          'Проверяем загрузчик. Система может запросить права администратора.';
    });
    final device = await widget.updater.probe();
    if (mounted) {
      setState(() {
        _device = device;
        _status = 'Загрузчик готов к записи.';
      });
    }
  });

  Future<void> _flash() => _run(() async {
    final file = _file!;
    final device = _device!;
    setState(() {
      _device = null;
      _sent = _total = 0;
      _status = 'Ожидаем разрешение системы на запись.';
    });
    await for (final event in widget.updater.flash(file, device)) {
      if (!mounted) continue;
      setState(() {
        switch (event.kind) {
          case 'preparing':
            _status = 'Проверяем устройство перед записью.';
          case 'writing':
          case 'progress':
            _writing = true;
            _status =
                'Идёт запись. Не отключайте кабель и не закрывайте приложение.';
            _sent = event.done;
            _total = event.total;
          case 'checking':
            _total = 0;
            _status = 'Ждём подтверждение загрузчика.';
          case 'success':
            _status =
                'Загрузчик подтвердил обновление (SUCCESS). Перезапустите прибор и проверьте его версию в настройках.';
          case 'unconfirmed':
            _status =
                'Все блоки переданы. Проверьте сообщение на приборе и версию в настройках после перезапуска.';
          case 'interrupted':
            _error = localUpdaterError(event.detail);
            _status =
                'Запись прервана. Войдите в IAP заново, прежде чем повторять обновление.';
          case 'error':
            _error = localUpdaterError(event.detail);
            _status = 'Запись не началась.';
        }
      });
    }
  });

  @override
  Widget build(BuildContext context) {
    final file = _file;
    final device = _device;
    final theme = Theme.of(context);
    return PopScope(
      canPop: !_working,
      child: AlertDialog(
        title: const Text('Обновление прошивки'),
        scrollable: true,
        content: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 480),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Зарядите FNIRSI 2C53T и подключите его кабелем передачи данных. '
                'Выключите его, затем включите с зажатой MENU, пока не появится экран обновления.',
              ),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: _working ? null : _select,
                icon: const Icon(Icons.folder_open),
                label: const Text('Выбрать файл .bin…'),
              ),
              if (file != null) ...[
                const SizedBox(height: 6),
                Text('${file.name} · ${file.bytes.length} байт'),
                Text(file.fileName, style: theme.textTheme.bodySmall),
              ],
              const SizedBox(height: 12),
              OutlinedButton.icon(
                onPressed: _working ? null : _probe,
                icon: const Icon(Icons.usb),
                label: Text(
                  device == null ? 'Проверить загрузчик' : 'Проверить снова',
                ),
              ),
              if (_status case final status?) ...[
                const SizedBox(height: 12),
                Text(status),
              ],
              if (_working) ...[
                const SizedBox(height: 10),
                LinearProgressIndicator(
                  value: _total > 0 ? _sent / _total : null,
                ),
              ],
              if (_error case final error?) ...[
                const SizedBox(height: 12),
                SelectableText(
                  error,
                  style: TextStyle(color: theme.colorScheme.error),
                ),
              ],
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: _working ? null : () => Navigator.of(context).pop(),
            child: const Text('Закрыть'),
          ),
          FilledButton(
            onPressed: _working || file == null || device == null
                ? null
                : _flash,
            child: const Text('Записать прошивку'),
          ),
        ],
      ),
    );
  }
}

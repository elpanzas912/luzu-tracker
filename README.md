# Luzu Tracker

Detecta qué videos de un canal de YouTube tienen **pauta publicitaria en Google Ads** y sigue sus **visitas** día a día.
Pensado para [LUZU TV](https://www.youtube.com/@luzutv/videos), pero funciona con cualquier anunciante del
[Google Ads Transparency Center](https://adstransparency.google.com/).

Solo usa la librería estándar de Python 3.9+. No necesita API keys ni login.

## Cómo funciona

1. **Anuncios:** consulta el endpoint interno del Transparency Center filtrando por día, plataforma YouTube y región
   Argentina, y extrae el `videoId` del preview de cada creativo.
2. **Visitas:** lee el contador de cada video desde su página de YouTube y guarda una medición en cada corrida.
3. **Dashboard:** genera un `dashboard.html` autocontenido (el del repo es un ejemplo con datos reales, bajalo y abrilo con doble clic) con los videos de cada día, una matriz día × video y
   las visitas ganadas por día (rojo = con pauta, gris = sin pauta).

Todo se guarda en un SQLite local (`luzu_tracker.db`).

## Uso

```
python luzu_tracker.py update                    # anuncios de ayer y hoy + visitas + dashboard
python luzu_tracker.py run [--day 2026-10-07]    # solo anuncios
python luzu_tracker.py backfill --from 2026-09-01
python luzu_tracker.py views                     # solo visitas
python luzu_tracker.py report --day 2026-10-07
python luzu_tracker.py csv --out pauta.csv
python luzu_tracker.py ui                        # genera y abre dashboard.html
```

Para otro anunciante: `--advertiser AR1234...` (el ID está en la URL de su página en el Transparency Center).

### Ejecución automática (Windows)

```powershell
$action  = New-ScheduledTaskAction -Execute (Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe') `
           -Argument 'luzu_tracker.py --log update' -WorkingDirectory 'C:\ruta\al\proyecto'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours 4) `
           -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName LuzuTracker -Action $action -Trigger $trigger -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

## Limitaciones

- **Endpoints no oficiales.** Pueden cambiar sin aviso; los códigos (región, plataforma, formato de fechas) están como
  constantes al inicio de `luzu_tracker.py`.
- **Límite de Google.** Si hace demasiadas consultas, Google redirige a su página anti-bot. El script lo detecta, no
  insiste y reintenta en el siguiente ciclo. No hagas ráfagas de consultas.
- **Días en hora del Pacífico**, no en hora argentina.
- **Solo ve pauta de Google Ads** a nombre del anunciante; no detecta sponsors dentro del video.
- **Visitas orgánicas + pagas juntas:** YouTube no las separa. El histórico empieza el día de la primera medición.
- Uso bajo tu responsabilidad: revisá los términos de servicio de Google y YouTube antes de usarlo a escala.

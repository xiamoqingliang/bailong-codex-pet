"""Small CMD entry points; uninstallation never uses the bundled runtime."""


def uninstall_command(edition, *, data_relative="data", script_relative="data\\uninstall.ps1"):
    if edition not in ("full", "native"):
        raise ValueError("Unknown pet edition")
    # The only generated token is a GUID. Package paths remain quoted Windows
    # arguments; PowerShell never evaluates a path as source code.
    text = (
        '@echo off\r\nsetlocal\r\n'
        f'set "BAILONG_UNINSTALL_DATA=%~dp0{data_relative}"\r\n'
        f'set "BAILONG_UNINSTALL_SOURCE=%~dp0{script_relative}"\r\n'
        'set "BAILONG_UNINSTALL_GUID="\r\n'
        'for /f "delims=" %%I in (\'powershell.exe -NoProfile -Command "[Guid]::NewGuid().ToString(\'N\')"\') do set "BAILONG_UNINSTALL_GUID=%%I"\r\n'
        'if not defined BAILONG_UNINSTALL_GUID exit /b 1\r\n'
        'set "BAILONG_UNINSTALL_COPY=%TEMP%\\Bailong-Uninstall-%BAILONG_UNINSTALL_GUID%.ps1"\r\n'
        'if exist "%BAILONG_UNINSTALL_COPY%" exit /b 1\r\n'
        'copy /y "%BAILONG_UNINSTALL_SOURCE%" "%BAILONG_UNINSTALL_COPY%" >nul\r\n'
        'if errorlevel 1 exit /b 1\r\n'
        'cd /d "%TEMP%"\r\n'
        '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -ExecutionPolicy Bypass '
        f'-File "%BAILONG_UNINSTALL_COPY%" -DataRoot "%BAILONG_UNINSTALL_DATA%" -Edition {edition} %*\r\n'
        'set "BAILONG_UNINSTALL_RESULT=%errorlevel%"\r\n'
        'del /q "%BAILONG_UNINSTALL_COPY%" >nul 2>&1\r\n'
        'if not "%BAILONG_UNINSTALL_RESULT%"=="0" pause\r\n'
        'exit /b %BAILONG_UNINSTALL_RESULT%\r\n'
    )
    return text.encode("ascii")

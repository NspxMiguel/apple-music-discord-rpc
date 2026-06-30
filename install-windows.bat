@echo off
title Apple Music Discord RPC - Installer
echo.
echo  Apple Music Discord RPC - Windows Installer
echo  =============================================
echo.

:: Check Python
set PYTHON=
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
    "%PROGRAMFILES%\Python314\pythonw.exe"
    "%PROGRAMFILES%\Python313\pythonw.exe"
    "%PROGRAMFILES%\Python312\pythonw.exe"
    "%PROGRAMFILES%\Python311\pythonw.exe"
) do (
    if exist %%p (
        set PYTHON=%%p
        goto :found_python
    )
)

:: Try PATH
where pythonw.exe >nul 2>&1
if %errorlevel%==0 (
    set PYTHON=pythonw.exe
    goto :found_python
)

echo  [ERRO] Python nao foi encontrado no seu computador.
echo.
echo  Instale o Python em: https://www.python.org/downloads/
echo  Marque a opcao "Add Python to PATH" durante a instalacao.
echo.
pause
exit /b 1

:found_python
echo  [OK] Python encontrado: %PYTHON%
echo.

:: Install destination
set DEST=C:\apple-music-rpc
echo  Instalando em: %DEST%
echo.

if not exist "%DEST%" mkdir "%DEST%"

:: Copy files
copy /y "%~dp0music-rpc-windows.py" "%DEST%\music-rpc-windows.py" >nul
if errorlevel 1 (
    echo  [ERRO] Falha ao copiar music-rpc-windows.py
    pause
    exit /b 1
)
echo  [OK] music-rpc-windows.py copiado

:: Install dependencies
echo.
echo  Instalando dependencias Python...
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%PROGRAMFILES%\Python314\python.exe"
    "%PROGRAMFILES%\Python313\python.exe"
) do (
    if exist %%p (
        set PIP=%%p
        goto :found_pip
    )
)
set PIP=python.exe
:found_pip

%PIP% -m pip install --quiet pypresence winrt-runtime "winrt-Windows.Media.Control" "winrt-Windows.Foundation" "winrt-Windows.Foundation.Collections" "winrt-Windows.Storage.Streams"
if errorlevel 1 (
    echo  [AVISO] Falha ao instalar dependencias. Tente manualmente:
    echo  pip install pypresence winrt-runtime winrt-Windows.Media.Control
) else (
    echo  [OK] Dependencias instaladas
)

:: Create startup shortcut in shell:startup
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set VBSFILE=%DEST%\start.vbs

echo Set WshShell = CreateObject("WScript.Shell") > "%VBSFILE%"
echo WshShell.Run Chr(34) ^& "%PYTHON%" ^& Chr(34) ^& " " ^& Chr(34) ^& "%DEST%\music-rpc-windows.py" ^& Chr(34), 0, False >> "%VBSFILE%"

echo Set oShell = CreateObject("WScript.Shell") > "%DEST%\create_shortcut.vbs"
echo Set oLink = oShell.CreateShortcut("%STARTUP%\Apple Music RPC.lnk") >> "%DEST%\create_shortcut.vbs"
echo oLink.TargetPath = "%VBSFILE%" >> "%DEST%\create_shortcut.vbs"
echo oLink.Description = "Apple Music Discord Rich Presence" >> "%DEST%\create_shortcut.vbs"
echo oLink.Save >> "%DEST%\create_shortcut.vbs"
cscript //nologo "%DEST%\create_shortcut.vbs"
del "%DEST%\create_shortcut.vbs"

echo  [OK] Atalho de inicializacao criado (inicia com o Windows)
echo.
echo  =============================================
echo  Instalacao concluida!
echo  O app vai iniciar agora e toda vez que o Windows ligar.
echo  =============================================
echo.

:: Launch now
start "" wscript.exe "%VBSFILE%"
echo  [OK] Apple Music RPC iniciado em background.
echo.
ping -n 4 127.0.0.1 >nul 2>&1

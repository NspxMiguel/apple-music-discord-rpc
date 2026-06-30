@echo off
powershell -WindowStyle Hidden -Command "& { Set-Location '%~dp0'; & 'C:\Users\User\AppData\Local\Programs\Python\Python314\pythonw.exe' music-rpc-windows.py }"

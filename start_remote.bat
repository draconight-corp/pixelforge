@echo off
title PixelForge - Remote Access
echo ============================================
echo   PixelForge Sprite Generator - Remote Mode
echo ============================================
echo.

:: Start the Python proxy server in background
echo [1/2] Starting local server on port 7880...
start /B python "%~dp0server.py" > nul 2>&1

:: Wait a bit for server to start
timeout /t 3 /noq > nul

:: Start Cloudflare tunnel
echo [2/2] Creating public tunnel...
echo.
echo    Waiting for public URL (look for the https:// link below)
echo    Share this link to access PixelForge from anywhere!
echo    Press Ctrl+C to stop.
echo.
"%~dp0cloudflared.exe" tunnel --url http://localhost:7880

; BattBench installer (NSIS 3, Unicode). Built by packaging/build.ps1:
;   makensis /DVERSION=1.0.0 packaging\installer.nsi
; Installs for the current user only (no administrator rights): program in %LOCALAPPDATA%\Programs\BattBench,
; Start menu entry, optional desktop shortcut, entry in "Installed apps". The measurement database lives in
; %LOCALAPPDATA%\BattBench and is kept on uninstall unless the user chooses to delete it.

Unicode true
!include "MUI2.nsh"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!define APP "BattBench"
!define EXE "BattBench.exe"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}"
!define DIST "..\build\dist\BattBench"

Name "${APP} ${VERSION}"
OutFile "..\build\BattBench-${VERSION}-setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "${UNINST_KEY}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "${APP} ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1033 "ProductName" "${APP}"
VIAddVersionKey /LANG=1033 "FileDescription" "${APP} Setup"
VIAddVersionKey /LANG=1033 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "GPL-3.0"

!define MUI_ICON "battbench.ico"
!define MUI_UNICON "battbench.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE}"
!define MUI_LANGDLL_ALLLANGUAGES

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "German"

LangString SecApp ${LANG_ENGLISH} "BattBench (required)"
LangString SecApp ${LANG_GERMAN} "BattBench (erforderlich)"
LangString SecDesktop ${LANG_ENGLISH} "Desktop shortcut"
LangString SecDesktop ${LANG_GERMAN} "Verknüpfung auf dem Desktop"
LangString Running ${LANG_ENGLISH} "BattBench is running. Please close it and click Retry."
LangString Running ${LANG_GERMAN} "BattBench läuft noch. Bitte schließen und dann „Wiederholen“ klicken."
LangString AskData ${LANG_ENGLISH} "Also delete your measurement database (sessions, batteries, models) in $LOCALAPPDATA\${APP}?"
LangString AskData ${LANG_GERMAN} "Auch die Messdatenbank (Vorgänge, Akkus, Modelle) in $LOCALAPPDATA\${APP} löschen?"

Function .onInit
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

Function un.onInit
  !insertmacro MUI_UNGETLANGUAGE
FunctionEnd

; the program files are replaced on update, so the app must not be running
!macro CheckRunning
  retry:
  ClearErrors
  FileOpen $0 "$INSTDIR\${EXE}" a
  IfErrors 0 free
  IfFileExists "$INSTDIR\${EXE}" 0 free
  MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(Running)" /SD IDCANCEL IDRETRY retry
  Abort
  free:
  FileClose $0
!macroend

Section "!$(SecApp)" SEC_APP
  SectionIn RO
  !insertmacro CheckRunning
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\_internal"
  File /r "${DIST}\*.*"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateShortcut "$SMPROGRAMS\${APP}.lnk" "$INSTDIR\${EXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${EXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "$(SecDesktop)" SEC_DESKTOP
  CreateShortcut "$DESKTOP\${APP}.lnk" "$INSTDIR\${EXE}"
SectionEnd

Section "Uninstall"
  !insertmacro CheckRunning
  Delete "$SMPROGRAMS\${APP}.lnk"
  Delete "$DESKTOP\${APP}.lnk"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"           ; the app's own settings (HKCU\Software\battbench) stay
  IfFileExists "$LOCALAPPDATA\${APP}\*.*" 0 done
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "$(AskData)" /SD IDNO IDNO done
  RMDir /r "$LOCALAPPDATA\${APP}"
  done:
SectionEnd

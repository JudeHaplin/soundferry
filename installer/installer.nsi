
; 声渡 SoundFerry 安装程序（用户级安装，不需要管理员权限）

!define APPNAME "声渡 SoundFerry"
!define APPNAME_EN "SoundFerry"
!define VERSION "1.5.0"
!define UNINSTKEY "SoundFerry"

!include "MUI2.nsh"
!include "Sections.nsh"

Name "${APPNAME}"
OutFile "..\SoundFerry_Setup_v${VERSION}.exe"
Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma

!define MUI_ICON "..\assets\soundferry.ico"
!define MUI_UNICON "..\assets\soundferry.ico"
!define MUI_ABORTWARNING

InstallDir "$LOCALAPPDATA\Programs\SoundFerry"
InstallDirRegKey HKCU "Software\${UNINSTKEY}" "InstallDir"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\SoundFerry.exe"
!define MUI_FINISHPAGE_RUN_TEXT "运行 声渡 SoundFerry"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; ---------------- 安装 ----------------
Section "-隐藏的必要文件"
  SetOutPath "$INSTDIR"
  File /r /x .git "..\dist\SoundFerry\*.*"
  WriteRegStr HKCU "Software\${UNINSTKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}" "Publisher" "SoundFerry"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}" "DisplayIcon" "$INSTDIR\SoundFerry.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}" "NoRepair" 1
  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "开机自动启动（推荐）" SecAuto
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APPNAME_EN}" '"$INSTDIR\SoundFerry.exe" --autostart'
SectionEnd

Section "开始菜单快捷方式" SecStart
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\SoundFerry.exe" "" "$INSTDIR\SoundFerry.exe"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\卸载 ${APPNAME}.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "桌面快捷方式" SecDesk
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\SoundFerry.exe" "" "$INSTDIR\SoundFerry.exe"
SectionEnd

LangString DESC_SecAuto ${LANG_SIMPCHINESE} "随 Windows 启动并在后台待命，双击桌面图标即可立即使用。"
LangString DESC_SecAuto ${LANG_ENGLISH} "Start SoundFerry with Windows (runs quietly in the background)."
LangString DESC_SecStart ${LANG_SIMPCHINESE} "在开始菜单创建快捷方式。"
LangString DESC_SecStart ${LANG_ENGLISH} "Create Start Menu shortcuts."
LangString DESC_SecDesk ${LANG_SIMPCHINESE} "在桌面创建快捷方式。"
LangString DESC_SecDesk ${LANG_ENGLISH} "Create a desktop shortcut."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAuto} $(DESC_SecAuto)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStart} $(DESC_SecStart)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesk} $(DESC_SecDesk)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function .onInit
  ; 若程序正在运行先结束
  nsExec::ExecToLog 'taskkill /IM SoundFerry.exe /F'
  Pop $0
FunctionEnd

; ---------------- 卸载 ----------------
Section "Uninstall"
  nsExec::ExecToLog 'taskkill /IM SoundFerry.exe /F'
  Pop $0
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APPNAME_EN}"
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTKEY}"
  DeleteRegKey HKCU "Software\${UNINSTKEY}"
SectionEnd

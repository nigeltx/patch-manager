; Inno Setup script. Installs for the current user only, so no admin prompt.
; Built by GitHub Actions:  iscc /DMyAppVersion=0.1.0 packaging\installer.iss
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{2F9D4B7E-8A13-4E6C-B0D2-5C7A9E1F3B44}
AppName=Patch Manager
AppVersion={#MyAppVersion}
AppPublisher=Nigel's studio
DefaultDirName={localappdata}\Programs\Patch Manager
DefaultGroupName=Patch Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=PatchManager-Setup-{#MyAppVersion}
SetupIconFile=..\patchmgr\icon.ico
UninstallDisplayIcon={app}\PatchManager.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\PatchManager\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Patch Manager"; Filename: "{app}\PatchManager.exe"
Name: "{userdesktop}\Patch Manager"; Filename: "{app}\PatchManager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PatchManager.exe"; Description: "Start Patch Manager"; Flags: nowait postinstall skipifsilent
; a silent install is an update from inside the app (patchmgr/updater.py): start the new version again
Filename: "{app}\PatchManager.exe"; Flags: nowait; Check: WizardSilent

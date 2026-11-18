; Inno Setup script for ClipSake. Build with build_windows.ps1 (project root),
; which runs PyInstaller first and passes the version from pyproject.toml.
; Manual build: ISCC.exe /DMyAppVersion=<version> installer\clipsake.iss

; The version is only kept in pyproject.toml, so it must be passed in.
#ifndef MyAppVersion
  #error Pass the version with /DMyAppVersion=<version>; build_windows.ps1 does this
#endif
#define MyAppName "ClipSake"
#define MyAppExeName "ClipSake.exe"

[Setup]
; Never change AppId: it is how upgrades find the existing installation.
AppId={{C5FDF95B-90C4-45DE-8BF7-D7688940A5E6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Install for the current user without admin rights by default; the user can
; choose an all-users install (which asks for admin) on the first page.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=ClipSake-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\timeline\assets\clipsake.ico
; The .exe carries the same icon, so this covers Windows' installed-apps list.
UninstallDisplayIcon={app}\{#MyAppExeName}
; Close a running copy of the app before replacing its files.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; This app's license, and the bundled software's (see THIRD_PARTY_NOTICES.md).
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\licenses\LGPL-3.0.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion

[InstallDelete]
; Clear the previous version's bundle so no stale libraries are left behind.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

; Inno Setup script for Cutout. Build the app folder first (packaging\cutout.spec), then:
;   ISCC.exe packaging\cutout.iss        ->  build\installer\Cutout-Setup-<version>.exe
; Keep the version in step with APP_VERSION in cutout_glass.py and packaging\version.txt.

#define AppName "Cutout"
#define AppVersion "1.0.0"
#define AppPublisher "rayokosaagh"
#define AppExe "Cutout.exe"
#define Root ".."

[Setup]
AppId={{8F3C2B6E-5B0E-4C1C-9D2A-7E4C1F3A9B21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Installs for the current user without admin rights; the user can choose all users instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#Root}\build\installer
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile={#Root}\assets\cutout.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=4
; ISCC is 32-bit: compress in a separate (64-bit) process, or ultra64 runs out of memory.
LZMAUseSeparateProcess=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#Root}\build\dist\Cutout\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; The same AppUserModelID the app sets, so a pinned taskbar icon and the running window group together.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "Cutout.App"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "Cutout.App"; Tasks: desktopicon

[Run]
; Compile the engine once now, so the first cut doesn't wait for it.
Filename: "{app}\{#AppExe}"; Parameters: "--warmup"; StatusMsg: "Preparing the background-removal engine (about a minute)..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The engine's compile cache and the log.
Type: filesandordirs; Name: "{localappdata}\{#AppName}"

[Code]
procedure DeleteModel(const Dir, Name: String);
begin
  DeleteFile(Dir + '\' + Name + '.onnx');
  DeleteFile(Dir + '\' + Name + '.onnx.part');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  Dir := ExpandConstant('{%USERPROFILE}') + '\.u2net';
  if UninstallSilent or not DirExists(Dir) then
    exit;
  if MsgBox('Also delete the background-removal models Cutout downloaded?' + #13#10 + #13#10 +
            'They are in ' + Dir + ' and can take over 1 GB. Other tools that use rembg ' +
            'share this folder, so choose No if you use any.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    DeleteModel(Dir, 'isnet-general-use');
    DeleteModel(Dir, 'u2net');
    DeleteModel(Dir, 'u2net_human_seg');
    DeleteModel(Dir, 'birefnet-general');
    DeleteModel(Dir, 'isnet-anime');
    DeleteFile(Dir + '\cutout-models.json');
    RemoveDir(Dir);   { only if nothing else is left in it }
  end;
end;

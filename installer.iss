; Eld's TTVDropMiner installer (Inno Setup 6)
; Run build.bat: it builds dist\EldsTTVDropMiner with PyInstaller and then compiles this script.

#define AppName "Eld's TTVDropMiner"
#define AppId "EldsTTVDropMiner"
#define AppExe "EldsTTVDropMiner.exe"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{BAE6FE8A-3130-4B8D-8146-B9D0F4DBDC72}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Programs\{#AppId}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; per-user install, no admin rights needed
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename={#AppId}-v{#AppVersion}-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
ShowLanguageDialog=auto
; the user must accept the Terms of Use (account risk, no warranty, noncommercial)
LicenseFile=TERMS.md

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
en.Extras=Extras:
en.DesktopIcon=Create a desktop shortcut
en.AutoStart=Start in the background (tray) when Windows starts
en.Launch=Launch {#AppName}
en.DeleteData=Also delete settings, login and logs?
tr.Extras=Ek seçenekler:
tr.DesktopIcon=Masaüstüne kısayol oluştur
tr.AutoStart=Windows açılınca arka planda (tepside) başlat
tr.Launch={#AppName} uygulamasını başlat
tr.DeleteData=Ayarlar, giriş bilgisi ve günlükler de silinsin mi?
es.Extras=Extras:
es.DesktopIcon=Crear un acceso directo en el escritorio
es.AutoStart=Iniciar en segundo plano (bandeja) al arrancar Windows
es.Launch=Iniciar {#AppName}
es.DeleteData=¿Eliminar también la configuración, el inicio de sesión y los registros?
ptbr.Extras=Extras:
ptbr.DesktopIcon=Criar um atalho na área de trabalho
ptbr.AutoStart=Iniciar em segundo plano (bandeja) com o Windows
ptbr.Launch=Abrir {#AppName}
ptbr.DeleteData=Excluir também as configurações, o login e os registros?
de.Extras=Extras:
de.DesktopIcon=Desktop-Verknüpfung erstellen
de.AutoStart=Beim Windows-Start im Hintergrund (Tray) starten
de.Launch={#AppName} starten
de.DeleteData=Auch Einstellungen, Anmeldung und Protokolle löschen?
fr.Extras=Options :
fr.DesktopIcon=Créer un raccourci sur le bureau
fr.AutoStart=Lancer en arrière-plan (zone de notification) au démarrage de Windows
fr.Launch=Lancer {#AppName}
fr.DeleteData=Supprimer aussi les paramètres, la connexion et les journaux ?
ru.Extras=Дополнительно:
ru.DesktopIcon=Создать ярлык на рабочем столе
ru.AutoStart=Запускать в фоне (в трее) вместе с Windows
ru.Launch=Запустить {#AppName}
ru.DeleteData=Удалить также настройки, данные входа и журналы?

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; GroupDescription: "{cm:Extras}"
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:Extras}"

[Files]
Source: "dist\{#AppId}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "TERMS.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "{#AppId}"; ValueData: """{app}\{#AppExe}"" --tray"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent

[Code]
procedure StopRunningApp();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM {#AppExe} /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopRunningApp();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // the start-up entry may also have been turned on from the tray menu
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppId}');
    // "No" is the default, so a silent uninstall never deletes the user's data
    if SuppressibleMsgBox(CustomMessage('DeleteData') + #13#10 + ExpandConstant('{localappdata}\{#AppId}'),
                          mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      DelTree(ExpandConstant('{localappdata}\{#AppId}'), True, True, True);
  end;
end;

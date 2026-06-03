#!/usr/bin/env bash
# Generate autounattend.xml + a bootable ISO for a fully unattended Win11-ARM64
# install on Tart/Apple-Virtualization (headless). Reusable turnkey artifact —
# destined for tools/ci/windows/ once proven.
#
# Injects the operator's pubkey SET into the sshd admin authorized_keys, enables
# OpenSSH Server, creates a local `admin` account, and bypasses TPM/SecureBoot/
# RAM/CPU checks (required for Win11 under AVF). Never bakes private keys.
set -euo pipefail
OUT_DIR="${1:-${TARTCI_WIN:-$HOME/.tartci/windows}}"
# Configurable key set (colon-separated paths via TARTCI_PUBKEYS); never bakes private keys.
IFS=: read -ra PUBKEYS <<< "${TARTCI_PUBKEYS:-$HOME/.ssh/id_ed25519.pub}"
mkdir -p "$OUT_DIR/media"

# Build the authorized_keys block (one key per line), then XML-escape for embedding.
AK=""
for f in "${PUBKEYS[@]}"; do [ -f "$f" ] && AK+="$(cat "$f")"$'\n'; done
# Each key becomes an `echo >> administrators_authorized_keys` in a FirstLogon cmd.
KEY_CMDS=""
order=20
while IFS= read -r k; do
  [ -z "$k" ] && continue
  KEY_CMDS+="
                <SynchronousCommand wcm:action=\"add\">
                    <Order>${order}</Order>
                    <CommandLine>cmd /c echo ${k}&gt;&gt; C:\\ProgramData\\ssh\\administrators_authorized_keys</CommandLine>
                </SynchronousCommand>"
  order=$((order+1))
done <<< "$AK"

cat > "$OUT_DIR/media/autounattend.xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <!-- Apple Virtualization presents storage as virtio-blk; Win11-ARM has no
           inbox driver, so Setup sees no disk. Load viostor (arm64) from the
           attached virtio-win ISO during WinPE, before DiskConfiguration. Drive
           letter of the virtio media is unpredictable in WinPE, so list
           candidates; Setup ignores paths that don't exist. -->
      <DriverPaths>
        <PathAndCredentials wcm:action="add" wcm:keyValue="1"><Path>D:\viostor\w11\ARM64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="2"><Path>E:\viostor\w11\ARM64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="3"><Path>F:\viostor\w11\ARM64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="4"><Path>G:\viostor\w11\ARM64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="5"><Path>H:\viostor\w11\ARM64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="6"><Path>D:\NetKVM\w11\ARM64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="7"><Path>E:\NetKVM\w11\ARM64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="8"><Path>F:\NetKVM\w11\ARM64</Path></PathAndCredentials>
      </DriverPaths>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>2</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>3</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>4</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassCPUCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>5</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassStorageCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>300</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>128</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Label>System</Label><Format>FAT32</Format></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>3</Order><PartitionID>3</PartitionID><Label>Windows</Label><Format>NTFS</Format><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom><MetaData wcm:action="add"><Key>/IMAGE/NAME</Key><Value>Windows 11 Pro</Value></MetaData></InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <ProductKey><Key>VK7JG-NPHTM-C97JM-9MPGT-3V66T</Key></ProductKey>
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>pulp-win</ComputerName>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>admin</Name><Group>Administrators</Group><DisplayName>admin</DisplayName>
            <Password><Value>admin</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <AutoLogon><Enabled>true</Enabled><Username>admin</Username><Password><Value>admin</Value><PlainText>true</PlainText></Password><LogonCount>3</LogonCount></AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add"><Order>1</Order><CommandLine>powershell -ExecutionPolicy Bypass -Command "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"</CommandLine></SynchronousCommand>
        <SynchronousCommand wcm:action="add"><Order>2</Order><CommandLine>powershell -Command "Set-Service -Name sshd -StartupType Automatic; Start-Service sshd"</CommandLine></SynchronousCommand>
        <SynchronousCommand wcm:action="add"><Order>3</Order><CommandLine>cmd /c if not exist C:\ProgramData\ssh mkdir C:\ProgramData\ssh</CommandLine></SynchronousCommand>${KEY_CMDS}
        <SynchronousCommand wcm:action="add"><Order>90</Order><CommandLine>powershell -Command "icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F'"</CommandLine></SynchronousCommand>
        <SynchronousCommand wcm:action="add"><Order>95</Order><CommandLine>powershell -Command "New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22"</CommandLine></SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
XML
echo "wrote $OUT_DIR/media/autounattend.xml ($(wc -l < "$OUT_DIR/media/autounattend.xml") lines)"

# Build a small bootable-data ISO with autounattend.xml at the root. Windows
# Setup auto-detects autounattend.xml on any attached removable media root.
hdiutil makehybrid -iso -joliet -default-volume-name "UNATTEND" \
  -o "$OUT_DIR/autounattend.iso" "$OUT_DIR/media" >/dev/null
echo "built $OUT_DIR/autounattend.iso"

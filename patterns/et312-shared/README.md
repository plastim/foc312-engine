# ET-312 shared routines

78 ErosLink routine files (`.elk`, 163 routines in all) written by ET-312 owners. In January 2011 ErosTek gave them
away as a free download, "as-is", on their blog
([their post, archived](https://web.archive.org/web/20111208144808/http://blog.erostek.com/2011/01/10/extra-eroslink-routines-free/)).
ErosTek's site is gone; the Internet Archive still has the zip.

The app lists them as **ET-312 shared routines** in the player, and the M5 remote gets them with its next
**Load patterns & settings**. Nothing needs downloading.

Not affiliated with or endorsed by ErosTek. ET-312 is a trademark of its owner.

## Where these files come from

They are the `.elk` files of
[Erostek312_routines.zip](https://web.archive.org/web/2011id_/http://www.erostek.com/Erostek312_routines.zip),
exactly as they are in the zip: the same bytes and the same names, nothing changed. The zip (98,048 bytes) has this
SHA-256:

```
6eb8a8e98165e1073fb3a0ff3fe2bde15a7646d6f0a42fc55e92b38088ac8070
```

The zip also holds 17 `.eis` files (settings saved from ErosLink's Interactive screen) and one `.doc` note; the app
doesn't use them, so they aren't here. To check this folder against the archived zip yourself, from the app folder:

```powershell
.\venv\Scripts\python.exe -m stimengine.et312.shared_routines
```

It downloads the zip, checks its SHA-256, and compares every file here with the zip's.

32 of these files are the same as ErosLink's own designer examples. If you have read ErosLink's installer in (see
INSTALL.md, step 8), those are listed once, under **ErosLink examples**.

## Removal

If you wrote one of these routines, or hold rights to them, and want them taken out of this project, open an issue at
[github.com/plastim/foc312-engine/issues](https://github.com/plastim/foc312-engine/issues) and they will be removed.

# WipEout Pure — Reverse Track Racing System

Questo documento spiega come funziona il **racing system** di WipEout Pure (PSP, versione EUR `UCES-00001`) e documenta i passaggi necessari per **invertire con successo la AI** (far correre le navi in senso anti-orario / "reverse").

---

## 1. Struttura dei file di tracciato (`.vex`)

Ogni tracciato è un file `.vex` (formato contenitore **VEXX**, version 4 su PSP). Un singolo file è un **albero di nodi tipizzati** che contiene tutto il necessario per l'ambiente del tracciato: geometria, collisioni, luci, suoni, camere e — cosa fondamentale — **il percorso di gara**.

### Header VEXX (16 byte)

```
struct VexxHeader {
    uint32_t version;         // Pure su PSP == 4
    uint32_t part1_length;    // lunghezza parte 1 (albero nodi)
    uint32_t part2_length;    // lunghezza parte 2 (texture)
    char     magic[4];        // "VEXX"
};
```

Dopo l'header, parte 1 (albero dei nodi) e poi parte 2 (texture). I nodi hanno un header di 16 byte:

```
struct TreeNodeHeader {
    uint32_t signature;   // tipo del nodo
    uint16_t header_len;  // lunghezza header + nome
    uint16_t unk1;
    uint32_t payload_len; // lunghezza del payload
    uint16_t nchildren;   // numero di figli
    uint16_t unk2;
};
```

Il nome del nodo è una stringa ASCII null-terminated subito dopo l'header.

### Nodi rilevanti per il racing system

| Nodo | Signature | Ruolo |
|------|-----------|-------|
| `wotrack_` | `0x36D` | **Il percorso di gara / racing line.** Definisce il percorso che la AI segue e il progresso del giro. |
| `wopoint*` / `wopointShape*` | `0x6D` / `0x381` | Waypoint di navigazione (punti nello spazio con dimensioni/direzione del "corridoio"). |
| `collision_floor` | `0x36B` | Geometria di collisione del pavimento (su cui le navi possono guidare). |
| `collision_wall` | `0x36C` | Geometria di collisione dei muri. |
| `collision_reset` | `0x37F` | Zone di reset. |
| `Start_Position_1` | `0x36E` | Posizione della griglia di partenza. |
| `starting_line_*` | `0x6D` | Linea di partenza/arrivo. |
| `Section*` | `0x37B` | Sezioni del tracciato (segmenti di riferimento). |
| `Track_Shape*` | `0x11E` | Mesh visibile del tracciato. |

---

## 2. Il nodo `wotrack_` (il cuore del racing system)

Il nodo `wotrack_` (signature `0x36D`, magic `WOtd` = `0x574F7464`) è **il percorso di gara**. È un **loop continuo** fatto di una o più **lane** (corsie) consecutive.

### Struttura del payload

```
+0x00  magic u32 = 'WOtd' (0x574F7464)
+0x04  sectionCount u32
+0x08  laneCount u32
+0x0C  laneGraphRowCount u32
+0x10  _unknown (48 byte, non ancora decodificato)
+0x40  lane table (laneCount * 32 byte)
+...   lane graph (laneGraphRowCount * 16 byte)
+...   punti (totalPoints * 112 byte)
```

### Lane descriptor (32 byte ciascuno)

```
+0x00  pointCount u32   // numero di punti di questa lane
+0x04  scale f32        // scala/larghezza media della corsia
+0x08  _unknown0 u32    // (sempre 0)
+0x0C  nextLaneGraphIdx u32  // indice nel lane graph per il "next"
+0x10  prevLaneGraphIdx u32  // indice nel lane graph per il "prev"
+0x14  _unknown1..3 u32      // (sempre 0)
```

### Punto di tracciato (`WoTrackPointV4`, 112 byte)

```
+0x00  position (4f)   // posizione world-space del centro corsia (xyz, w=0)
+0x10  right    (4f)   // vettore trasversale (lato), unit
+0x20  down     (4f)   // vettore verso il basso (superficie), unit = -normal
+0x30  forward  (4f)   // direzione di marcia, unit   <-- CHIAVE per il reverse
+0x40  param    (f32)  // parametro arco-lunghezza (monotono crescente ~0..1)
+0x44  leftMetric (f32)// distanza al bordo sinistro
+0x48  rightMetric(f32)// distanza al bordo destro
+0x4C  _unknown0 (f32)
+0x50  _unknown1 (f32)
+0x54  tailMeta (28 byte)
```

### Proprietà fondamentali dei vettori

I tre vettori `right`, `down`, `forward` devono formare una **base ortonormale**:

- `right = forward × down`  (prodotto vettoriale)
- `down = -surfaceNormal`  (l'up della nave è `-down`)
- Tutti e tre sono **vettori unitari** (magnitudine ~1.0)

Il tool di validazione (da `PierreBelmondo/vscode-wipeout`) verifica che la magnitudine di ogni vettore sia ~1.0 e che formino una base ortonormale. **Se questa proprietà si rompe, la nave IA punta male e compaiono bug visivi (luci/orientamento).**

---

## 3. Come la AI usa il `wotrack_`

La AI (e il sistema di progresso gara) usa il nodo `wotrack_` per:

1. **Determinare il percorso** da seguire (la racing line).
2. **Calcolare il progresso del giro** tramite il `param` (arco-lunghezza).
3. **Contare i giri** quando il `param` "wrap-a" (passa da ~1 a 0).
4. **Passare da una lane all'altra** tramite il **lane graph** (le connessioni `next`/`prev`).

Il `forward` di ogni punto è la **direzione di marcia**. Quando la nave segue il percorso, si orienta lungo il `forward` del punto corrente.

> **Nota importante:** in questo progetto abbiamo verificato che, per WipEout Pure, la AI usa **il nodo `wotrack_`** per la navigazione (non i `wopoint*`). Infatti, invertendo solo il `wotrack_`, le navi corrono in reverse **senza** dover toccare i `wopoint`.

---

## 4. Passaggi per invertire con successo la AI

Dopo molti tentativi, la soluzione corretta per invertire il percorso è la seguente.

### 4.1 Requisito preliminare: tracciato percorribile al contrario

Il file di base **deve** essere un tracciato percorribile in entrambe le direzioni. Alcuni tracciati vanilla hanno **salti/rampe** che, al contrario, non sono percorribili. In questo progetto abbiamo usato un file modificato (`modmesh3.vex`) in cui le **collisioni** (`collision_floor`, `collision_wall`) e la **mesh** sono state modificate per rendere il salto percorribile in reverse.

> ⚠️ Il nodo `wotrack_` del file modificato è identico al vanilla. Solo collisioni/mesh sono diverse.

### 4.2 La soluzione: invertire i punti dentro ogni lane, SENZA scambiare le lane

**Il passo chiave che ha risolto il problema** è stato:

1. **Invertire l'ordine dei punti DENTRO ogni lane** (il percorso scorre al contrario).
2. **NON scambiare l'ordine delle lane** (mantenere lane[0] con i suoi pointCount, lane[1] con i suoi).
3. **Mantenere invariati** i lane descriptor (`next`/`prev`) e il **lane graph**.

Perché questo è importante:
- Se si **scambiano le lane**, il **lane graph** diventa incoerente (punta a lane con pointCount sbagliati) → la AI al passaggio tra lane va alla lane sbagliata → **punta il muro**.
- Se si **invertono solo i punti dentro ogni lane** e si mantiene la struttura delle lane, il lane graph resta valido → la AI passa correttamente da lane[0] a lane[1].

### 4.3 Trasformazione dei vettori (mantenendo la base ortonormale)

Per ogni punto, dopo l'inversione dell'ordine:

```python
forward_new = -forward_old   # direzione di marcia invertita
down_new    = down_old       # superficie invariata
right_new   = normalize(forward_new × down_new)  # base ortonormale mantenuta
```

Inoltre:
- **Scambiare `leftMetric` e `rightMetric`** (i bordi si invertono nel reverse).

> ⚠️ **Attenzione:** lo scambio di `leftMetric`/`rightMetric` va fatto **solo se necessario**. Nel nostro caso il problema del puntamento verso il muro in un punto specifico era causato dallo **scambio delle lane**, non dallo scambio delle metriche. La variante finale **non scambia le lane** e mantiene le metriche coerenti.

### 4.4 Ricostruire il `param`

Il `param` deve essere **monotono crescente** lungo il nuovo senso di marcia. Va ricostruito come **arco-lunghezza normalizzato** (distanza cumulativa tra punti consecutivi, escludendo eventuali "crossover" — distanze anomale > 10x la mediana, che sono teleport del circuito e non lunghezza reale).

```python
# distanze tra punti consecutivi (loop chiuso)
# escludi distanze > 10 * mediana (crossover)
# param[i] = cumulativa / totale
```

### 4.5 Start position (opzionale, per lo spawn)

Se serve anche lo spawn corretto, la start position (`Start_Position_1`) va spostata al punto del `wotrack_` con `param=0` (l'inizio del giro reverse). In questo progetto però lo spawn vanilla funzionava già, quindi non è stato necessario.

---

## 5. Il tool finale

Il tool che ha funzionato è `reverse_noswap_lanes.py`, che:

1. Inverte i punti **dentro ogni lane** (senza scambiare l'ordine delle lane).
2. Mantiene **invariati** i lane descriptor e il lane graph.
3. Inverte `forward`, mantiene `down`, ricomputa `right = forward × down`.
4. Scambia `leftMetric`/`rightMetric`.
5. Ricostruisce il `param` monotono.

### Validazione

Il file risultante deve passare questi controlli:
- Vettori `right`, `down`, `forward` **unitari** (magnitudine ~1.0).
- `right = forward × down` (base ortonormale).
- `param` **monotono crescente** (0 salti > 0.05).
- Il file `.vex` ha **la stessa dimensione** dell'originale (patch chirurgica, non ricostruzione).

---

## 6. L'addon Blender (`vex_blender_addon.py`) — import/export della geometria

Oltre al tool Python di reverse, il progetto include un **addon per Blender** che permette di importare ed esportare la geometria di collisione e la mesh visibile dei tracciati `.vex` direttamente dentro Blender — senza formati intermedi e senza fare conti manuali sugli offset.

### Come funziona

L'addon modifica i file `.vex` in modo **chirurgico**: tocca **solo** i range di byte che memorizzano le posizioni dei vertici. Tutto il resto del file (header dei nodi, dati di triangoli/indici, texture, materiali, nodi non correlati) viene copiato **byte-per-byte, invariato**. Questo significa che il tool non deve comprendere (né preservare la correttezza di) ogni parte del formato VEXX — deve solo localizzare in modo affidabile le posizioni dei vertici, cosa validata dal round-trip di file reali modificati fino a un risultato byte-identico.

### Tipi di nodi compresi

| Nodo | Signature | Ruolo |
|------|-----------|-------|
| `collision_floor` / `collision_wall` / `collision_reset` | `0x36B` / `0x36C` / `0x37F` | Geometria invisibile per la fisica. Posizioni come float a 32 bit. |
| Mesh nodes (`Track_Shape*`, `polySurface*`) | `0x11E` | Geometria *visibile* del tracciato. Posizioni quantizzate a 16 bit + fattore di scala, in formato vertex nativo della PSP GPU, organizzate in triangle strips. |
| Pads (`speedup_pad_nolightparentShape` / `weapon_pad_nolightparentShape`) | `0x36F` / `0x370` | Pad di velocità e di armi. Sottoclasse del formato mesh (layout binario identico, solo signature diversa). La geometria è definita una volta in spazio locale/template e istanziata alla posizione/orientamento reale tramite un nodo wrapper con matrice 4x4. L'addon espone la transform del wrapper come transform dell'oggetto pad (non la cuoce nella mesh), così spostare/ruotare il pad in Object Mode ne cambia la posizione reale in-game. L'origine è al centro geometrico del pad. |
| Texture nodes | `0x373` | Read-only, per la visualizzazione. L'addon decodifica le texture reali del gioco (colore indicizzato/palettizzato, con layout "swizzle" della memoria texture PSP) e le coordinate UV, costruendo un materiale Blender per ogni texture realmente referenziata dalle mesh importate. |

### Import

`VEX Tools panel → Import VEX Track` legge un `.vex` e costruisce gli oggetti Blender, organizzati sotto una collection **`VEX Track`**:

- `VEX - Collisions Floor` / `Wall` / `Reset` / `Other`
- `VEX - Track Mesh` (quando i nodi mesh seguono la convenzione `Track_Shape*`, un oggetto unificato)
- `VEX - Track Surface (guessed)` / `VEX - Landscape (guessed)` (nodi mesh NON coperti dalla convenzione `Track_Shape*`, importati come oggetti separati e ordinati con un'euristica sul nome)
- `VEX - Pads (Speedup)` / `VEX - Pads (Weapon)`

Ogni oggetto importato porta proprietà custom nascoste (`vex_fmt`, `vex_offsets`, `vex_scales`, `vex_orig_vcount`) che registrano dove nel file originale proveniva ogni vertice. Queste rendono possibile l'export.

### Edit

Spostare i vertici come qualsiasi mesh Blender. Questa è l'unica operazione supportata.

### Export

`VEX Tools panel → Export VEX Track` riapre il file sorgente originale (il percorso è ricordato automaticamente) e, per ogni oggetto con metadati VEX, riscrive le posizioni correnti dei vertici in una copia del file, agli offset byte originali. Tutto il resto è lasciato intatto. Si sceglie il percorso di output, quindi l'originale non viene mai sovrascritto.

### Come funziona l'export internamente

- Ad ogni vertice, in import, viene salvato l'offset byte esatto (e, per i nodi mesh, la scala di quantizzazione per chunk).
- In export, l'addon non rigenera né reinterpreta la struttura del file: apre i byte originali e sovrascrive solo quei range specifici con le posizioni correnti (eventualmente editate), riconvertendo dallo spazio di coordinate di Blender e, per i nodi mesh, ri-quantizzando a 16 bit con la scala registrata.
- **Convenzione degli assi:** il gioco usa Y-up, Blender Z-up. Import/export applicano una conversione esatta e mutuamente inversa (`(x,y,z) → (x,-z,y)` in import, l'inversa in export) così le modifiche fanno round-trip corretto e il tracciato appare dritto nel viewport di Blender.

### Cosa NON fare

L'addon abbina i vertici **1:1, per ordine di indice**, contro una lista di offset registrati in import. Qualsiasi cosa cambi il **numero** o l'**ordine** dei vertici rompe silenziosamente quella corrispondenza:

- **Mai aggiungere/rimuovere vertici** (no Extrude, Merge, Delete, Subdivide, Loop Cut, Boolean, Decimate, ecc.). Solo lo spostamento è supportato.
- **Mai unire/joinare** due oggetti VEX (`Ctrl+J`) — mescola dati di due mappature di offset diverse.
- **Spostare/ruotare un intero oggetto** (Object Mode, `G`/`R`/`S`) è sicuro e correttamente esportato (l'export legge la posizione world-space finale attraverso la transform). Eccezione: i pad espongono la transform del wrapper come transform dell'oggetto.
- **Attenzione ai duplicati** (`Shift+D`): copia le proprietà custom, quindi due oggetti possono rivendicare gli stessi vertici sorgente. All'export, l'ultimo processato sovrascrive l'altro.
- **Cancellare un intero oggetto è sicuro** — l'export semplicemente non tocca quella geometria.
- **Uscire da Edit Mode prima di esportare** (l'addon lo fa automaticamente come rete di sicurezza).
- **Tenere un backup dell'originale.**

### Limiti noti

- **Non tutti i nodi vengono parsati.** Alcuni tipi sembrano nodi di collisione/mesh ma non lo sono (es. alcune varianti `collision_wall_*` sono nodi transform/instance che referenziano geometria condivisa altrove). Vengono rilevati e saltati in sicurezza, con un warning.
- **Alcuni nodi con nome collision sono vuoti per design** (locator Maya residui, es. `collision_wall_freestyleN`). Il gioco smista per tipo di nodo, non per nome.
- **La divisione track/landscape (modalità fallback) è un'indovinata sul nome.** I file senza convenzione `Track_Shape*` non hanno un modo affidabile, dai soli dati binari, di distinguere la superficie percorribile dallo scenario.
- **L'assemblaggio delle triangle strip è una ricostruzione**, non un porting byte-esatto del renderer. Influenza solo l'aspetto in Blender — l'export non scrive mai dati di topologia, solo posizioni, quindi non può corrompere il file.
- **I riferimenti a mesh esterni/condivisi non sono risolti.**
- **Testato solo su WipEout Pure (VEXX v4).** Pulse (v6) usa uno stride leggermente diverso in alcuni punti; supporto parziale non testato.
- **Le texture sono solo per visualizzazione e non vengono mai riscritte.** Solo le posizioni dei vertici fanno round-trip nel `.vex`.

### Crediti

Questo addon si basa sul reverse-engineering di altri:

- **[PierreBelmondo/vscode-wipeout](https://github.com/PierreBelmondo/vscode-wipeout)** (MIT) — l'estensione VS Code per modding WipEout. Il suo sorgente TypeScript (`core/formats/vexx/v4/collision.ts`, `mesh.ts`, `texture.ts`, `speedup_pad.ts`, `weapon_pad.ts`, ecc.) è la ground truth da cui sono portati i parser binari.
- **[thp.io](https://thp.io) (Thomas Perl)** — reverse-engineering pubblico del formato container VEXX (`walk-vex.py`, 2022).
- La community homebrew PSP per la documentazione del formato vertex `sceGu`.
- **droastedcat** — maintainer, e chi ha fatto tutto il testing reale (in Blender e in-game).

---

## 7. Sintomi e cause (tabella di troubleshooting)

| Sintomo | Causa | Soluzione |
|---------|-------|-----------|
| Navi puntano il muro in un punto specifico (al passaggio tra lane) | **Lane scambiate** → lane graph incoerente | Non scambiare l'ordine delle lane |
| Bug visivi (luci/orientamento) su tutte le navi | Base ortonormale rotta (`right`, `down`, `forward` incoerenti) | Ricomputare `right = forward × down`, mantenere `down` |
| Navi corrono in forward invece di reverse | `forward` non negato | Negare `forward` |
| Navi penetrano i muri | Direzione di navigazione sbagliata | Invertire i punti dentro ogni lane |
| Lap counter sbagliato (doppio incremento) | `param` non monotono | Ricostruire `param` come arco-lunghezza |

---

## 8. La deformazione del Quake (limite noto)

La weapon **Quake** genera un'onda sismica che viaggia lungo il circuito, producendo due effetti:

- **Danno** (logica di gioco): **funziona correttamente in reverse** ✓
- **Deformazione visiva** (particle system): **resta forward** ✗ (limite noto)

### Perché

Il nodo `Quake` (signature `0x379`, nome `Quake`, un `Quake_ImportNode`) **non contiene il percorso** — importa i particle system `WO_QUAKE.POB` / `WO_TRACK_ROCK_DEBRIS.POB` da `Data\psys\`. La **direzione della deformazione è calcolata a runtime** dal codice (`~QUAKETRAVEL`), non dal file di tracciato.

### Struttura del nodo Quake (payload, 249 record da 96 byte)

```
+0x00  count u32 (= 249)  // numero di record
+0x1C  link_a  (u16)      // lista concatenata strutturale (next) — NON invertire
+0x1E  sentinella -1
+0x20  link_b  (u16)      // seconda lista concatenata — NON invertire
+0x22  sentinella -1
+0x2A  indice record
+0x30  offset (u16)
+0x32  ramo (u16, 0/1/2)
+0x38  conteggio (u16)
+0x3C  t0 (f32)  // unico campo di progresso significativo, letto dal codice
+0x40  t1 (f32)  // fine intervallo (t0 <= t1)
```

### Cosa NON funziona (testato e scartato)

| Variante | Modifica | Esito |
|----------|----------|-------|
| `a` | Invertire `link_a` | ❌ lag + onde irregolari (link è strutturale) |
| `ab` | Invertire `link_a` + `link_b` | ❌ lag + onde irregolari (come `a`) |
| `t` | Specchiare `t0`/`t1` | ❌ nessun effetto sulla direzione |
| `t0` / `t1` | Specchiare un singolo campo | ❌ strutturalmente invalidi (rompe `t0 <= t1`) |

### Conclusione

- `link_a`/`link_b` sono i **puntatori strutturali** della lista concatenata dei particle: invertirli corrompe il sistema.
- La direzione della deformazione **non** è nei record dell'header (dove vive `+0x3C`), ma nei **dati per-particle** nella regione geometria (offset `249*96 = 23904`, ~171808 byte), che iniziano con una **tabella separata di 2832 valori in [0,1]** (curve di animazione per-particle).
- Modificare il nodo Quake nel `.vex` da solo **non** ha risolto la direzione.

> **Limite accettato:** la deformazione visiva del Quake resta forward. Il **danno** (gameplay) e le **luci rosse** funzionano correttamente in reverse. Il fix richiederebbe una patch al codice runtime (`~QUAKETRAVEL`) — non eseguita per stabilità.

---

## 9. Riferimenti

- **Formato VEXX** — `thp.io/2022/vexx-file-format.html` (Thomas Perl, `walk-vex.py`)
- **Parser WO_TRACK** — `PierreBelmondo/vscode-wipeout` (`core/formats/vexx/v4/wo_track.ts`)
- **Addon Blender** — `dRoastedCat/Wipeout-tools` (`vex_blender_addon.py`), usato per visualizzare le lane
- **Strumento WAD** — `p.py` (wadutil) per estrarre/ricreare il `Data.wad`
- **Note sull'analisi del Quake** — `data/environments/12_sol_2/QUAKE_FINDINGS.md`

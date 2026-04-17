//******************************************************************************
// Copyright (c) 2019 - 2019, The Regents of the University of California (Regents).
// All Rights Reserved. See LICENSE and LICENSE.SiFive for license details.
//------------------------------------------------------------------------------

package chipyard

import chisel3._

import freechips.rocketchip.prci._
import org.chipsalliance.cde.config.{Field, Parameters}
import freechips.rocketchip.devices.tilelink._
import freechips.rocketchip.devices.debug.{HasPeripheryDebug, ExportDebug, DebugModuleKey}
import sifive.blocks.devices.uart.{HasPeripheryUART, PeripheryUARTKey}
import freechips.rocketchip.diplomacy._
import freechips.rocketchip.tile._
import freechips.rocketchip.tilelink._
import freechips.rocketchip.interrupts._
import freechips.rocketchip.util._
import freechips.rocketchip.subsystem._
import freechips.rocketchip.amba.axi4._

import testchipip.serdes.{CanHavePeripheryTLSerial, SerialTLKey}

trait CanHaveHTIF { this: BaseSubsystem =>
  // Advertise HTIF if system can communicate with fesvr
  if (this match {
    case _: CanHavePeripheryTLSerial if (p(SerialTLKey).size != 0) => true
    case _: HasPeripheryDebug if (!p(DebugModuleKey).isEmpty && p(ExportDebug).dmi) => true
    case _ => false
  }) {
    ResourceBinding {
      val htif = new Device {
        def describe(resources: ResourceBindings): Description = {
          val compat = resources("compat").map(_.value)
          Description("htif", Map(
            "compatible" -> compat))
        }
      }
      Resource(htif, "compat").bind(ResourceString("ucb,htif0"))
    }
  }
}

// This trait adds the "chosen" node to DTS, which
// can be used to pass information to OS about the earlycon
case object ChosenInDTS extends Field[Boolean](true)
trait CanHaveChosenInDTS { this: BaseSubsystem =>
  if (p(ChosenInDTS)) {
    this match {
      case t: HasPeripheryUART if (!p(PeripheryUARTKey).isEmpty) => {
        val chosen = new Device {
          def describe(resources: ResourceBindings): Description = {
            val stdout = resources("stdout").map(_.value)
            Description("chosen", resources("uart").headOption.map { case Binding(_, value) =>
              "stdout-path" -> Seq(value)
            }.toMap)
          }
        }
        ResourceBinding {
          t.uarts.foreach(u => Resource(chosen, "uart").bind(ResourceAlias(u.device.label)))
        }
      }
      case _ =>
    }
  }
}

import saturn.rocket.{RocketTCMKey, RocketSGTCMKey, CanHaveRocketTCM}

/** Instantiate periphery TCM(s) specified by RocketTCMKey / RocketSGTCMKey */
trait CanHaveRocketTCMSubsystem { this: BaseSubsystem with InstantiatesHierarchicalElements =>
  import freechips.rocketchip.tilelink.{TLRAM, TLFragmenter}
  import freechips.rocketchip.diplomacy.AddressSet
  import shuttle.dmem.SGTCM

  private class RocketTCMBank(
    address: AddressSet,
    beatBytes: Int,
    devOverride: MemoryDevice,
    devName: String
  )(implicit p: Parameters) extends ClockSinkDomain(ClockSinkParameters())(p) {
    val ram = LazyModule(new TLRAM(
      address = address,
      beatBytes = beatBytes,
      devOverride = Some(devOverride),
      devName = Some(devName)
    ))
    val node = ram.node
  }

  private class RocketSGTCMMem(
    address: AddressSet,
    beatBytes: Int,
    devOverride: MemoryDevice,
    devName: String
  )(implicit p: Parameters) extends ClockSinkDomain(ClockSinkParameters())(p) {
    val mem = LazyModule(new SGTCM(
      address = address,
      beatBytes = beatBytes,
      devOverride = Some(devOverride),
      devName = Some(devName)
    ))
    val node = mem.node
    val sgnode = mem.sgnode
  }

  private class RocketSGSidebandXbar(implicit p: Parameters) extends ClockSinkDomain(ClockSinkParameters())(p) {
    val xbar = LazyModule(new TLXbar)
    val node = xbar.node
  }

  val rocketTiles = totalTiles.values.collect { case r: RocketTile => r }

  rocketTiles.foreach { tile =>
    val tcmParams = tile.p(RocketTCMKey)
    val sgtcmParams = tile.p(RocketSGTCMKey)
    val tileId = tile.tileId
    val sbus = locateTLBusWrapper(SBUS)

    // 1. Tightly Coupled Memory (TCM)
    tcmParams.foreach { params =>
      val device = new MemoryDevice
      for (b <- 0 until params.banks) {
        val bankBase = params.base + b * p(CacheBlockBytes)
        val bankMask = params.size - 1 - (params.banks - 1) * p(CacheBlockBytes)
        val tcm = LazyModule(new RocketTCMBank(
          address = AddressSet(bankBase, bankMask),
          beatBytes = tile.masterPortBeatBytes,
          devOverride = device,
          devName = s"Core $tileId TCM bank $b"
        ))
        tcm.clockNode := sbus.fixedClockNode
        sbus.coupleTo(s"core_${tileId}_tcm_bank_${b}") {
          tcm.node := TLFragmenter(tile.masterPortBeatBytes, p(CacheBlockBytes)) := TLBuffer() := _
        }
      }
    }

    // 2. Sideband Global TCM (SGTCM) - specifically for Saturn
    sgtcmParams.foreach { params =>
      val device = new MemoryDevice
      val sgtcm = LazyModule(new RocketSGTCMMem(
        address = AddressSet(params.base, params.size - 1),
        beatBytes = params.banks, // SGTCM banks are beatBytes in the constructor
        devOverride = device,
        devName = s"Core $tileId SGTCM"
      ))
      sgtcm.clockNode := sbus.fixedClockNode
      val sgtcmXbar = LazyModule(new RocketSGSidebandXbar)
      sgtcmXbar.clockNode := sbus.fixedClockNode
      sbus.coupleTo(s"core_${tileId}_sgtcm") {
        sgtcm.node := TLWidthWidget(tile.masterPortBeatBytes) := _
      }
      sgtcm.sgnode :*= sgtcmXbar.node

      // SGTCM specialized sideband connection to the instantiated Saturn vector unit
      tile.vector_unit.collect { case v: saturn.rocket.SaturnRocketUnit => v }.foreach { v =>
        v.sgNode.foreach { n => sgtcmXbar.node :=* n }
      }
    }
  }
}

class ChipyardSubsystem(implicit p: Parameters) extends BaseSubsystem
    with InstantiatesHierarchicalElements
    with HasTileNotificationSinks
    with HasTileInputConstants
    with CanHavePeripheryCLINT
    with CanHavePeripheryPLIC
    with HasPeripheryDebug
    with HasHierarchicalElementsRootContext
    with HasHierarchicalElements
    with CanHaveHTIF
    with CanHaveChosenInDTS
  with CanHaveRocketTCMSubsystem
{
  def coreMonitorBundles = totalTiles.values.map {
    case r: RocketTile => r.module.core.rocketImpl.coreMonitorBundle
    case b: boom.v3.common.BoomTile => b.module.core.coreMonitorBundle
    case b: boom.v4.common.BoomTile => b.module.core.coreMonitorBundle
  }.toList

  // No-tile configs have to be handled specially.
  if (totalTiles.size == 0) {
    // no PLIC, so sink interrupts to nowhere
    require(!p(PLICKey).isDefined)
    val intNexus = IntNexusNode(sourceFn = x => x.head, sinkFn = x => x.head)
    val intSink = IntSinkNode(IntSinkPortSimple())
    intSink := intNexus :=* ibus.toPLIC

    // avoids a bug when there are no interrupt sources
    ibus { ibus.fromAsync := NullIntSource() }

    // Need to have at least 1 driver to the tile notification sinks
    tileHaltXbarNode := IntSourceNode(IntSourcePortSimple())
    tileWFIXbarNode := IntSourceNode(IntSourcePortSimple())
    tileCeaseXbarNode := IntSourceNode(IntSourcePortSimple())
  }

  // Relying on [[TLBusWrapperConnection]].driveClockFromMaster for
  // bus-couplings that are not asynchronous strips the bus name from the sink
  // ClockGroup. This makes it impossible to determine which clocks are driven
  // by which bus based on the member names, which is problematic when there is
  // a rational crossing between two buses. Instead, provide all bus clocks
  // directly from the allClockGroupsNode in the subsystem to ensure bus
  // names are always preserved in the top-level clock names.
  //
  // For example, using a RationalCrossing between the Sbus and Cbus, and
  // driveClockFromMaster = Some(true) results in all cbus-attached device and
  // bus clocks to be given names of the form "subsystem_sbus_[0-9]*".
  // Conversly, if an async crossing is used, they instead receive names of the
  // form "subsystem_cbus_[0-9]*". The assignment below provides the latter names in all cases.
  Seq(PBUS, FBUS, MBUS, CBUS).foreach { loc =>
    tlBusWrapperLocationMap.lift(loc).foreach { _.clockGroupNode := allClockGroupsNode }
  }
  override lazy val module = new ChipyardSubsystemModuleImp(this)
}

class ChipyardSubsystemModuleImp[+L <: ChipyardSubsystem](_outer: L) extends BaseSubsystemModuleImp(_outer)
    with HasHierarchicalElementsRootContextModuleImp {
  override lazy val outer = _outer
}

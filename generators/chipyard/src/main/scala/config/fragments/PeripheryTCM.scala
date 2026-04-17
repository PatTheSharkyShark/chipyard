package chipyard.config

import org.chipsalliance.cde.config.{Config, Field}
import freechips.rocketchip.diplomacy.AddressSet

/** Parameters describing a periphery TCM to instantiate in the subsystem */
case class PeripheryTCMParams(address: BigInt = 0x70000000L, size: BigInt = 64L << 10, beatBytes: Int = 16, name: String = "tcm")

/** Config key for periphery TCMs */
case object PeripheryTCMKey extends Field[Seq[PeripheryTCMParams]](Nil)

/** Config fragment to add a periphery TCM */
class WithPeripheryTCM(params: PeripheryTCMParams) extends Config((site, here, up) => {
  case PeripheryTCMKey => up(PeripheryTCMKey) ++ Seq(params)
})

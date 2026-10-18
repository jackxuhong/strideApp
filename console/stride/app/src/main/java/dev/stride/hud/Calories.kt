package dev.stride.hud

/**
 * Calories from the ACSM metabolic equations, worked from the belt rather than
 * taken from the board.
 *
 * The board has its own count, but it is computed against a weight STRIDE has
 * never told it, so it was the same number for everybody. These need the
 * walker's weight and nothing else; without one there is no figure at all
 * rather than a guess.
 *
 * Integrated per metre of odometer instead of per poll: oxygen cost per minute
 * is speed × (cost per metre), so summing over metres walked is the same sum
 * and rides on the one counter the board keeps exactly.
 *
 * "Active" is the work above resting, as Apple Health counts it. "Total" adds
 * the resting 3.5 ml/kg/min over the time on the belt.
 *
 * Assumes hands off the rails. Holding them does a fair share of the work, and
 * the belt cannot see that.
 */
object Calories {
    /**
     * ACSM's walking equation is valid to about 6 km/h and the running one from
     * about 8; between them it depends on gait, which the belt cannot see.
     * ponytail: fixed crossover, make it per person if somebody jogs at 6.5.
     */
    const val RUN_KPH = 7.0
    private const val REST_ML_KG_MIN = 3.5
    private const val KCAL_PER_LITRE_O2 = 5.0

    /** Oxygen above resting, ml per kg, for one metre of belt at this speed and grade. */
    fun o2PerMetre(kph: Double, gradePct: Double): Double {
        // The equations are uphill only. Downhill is cheaper than flat, but not
        // by an amount they model, so it counts as flat.
        val g = (gradePct / 100.0).coerceAtLeast(0.0)
        return if (kph >= RUN_KPH) 0.2 + 0.9 * g else 0.1 + 1.8 * g
    }

    fun kcal(o2MlPerKg: Double, kg: Double): Double = o2MlPerKg * kg / 1000.0 * KCAL_PER_LITRE_O2

    fun restingKcal(seconds: Double, kg: Double): Double = kcal(REST_ML_KG_MIN * seconds / 60.0, kg)
}

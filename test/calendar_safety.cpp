#include "types.h"
#include <cassert>
int main() {
  Recurrence rule;
  rule.Interval = 65535; rule.Count = 65535;
  assert(std::string(rule).find("INTERVAL=65535;COUNT=65535") != std::string::npos);
  AlarmTrigger trigger;
  for (const char *text : {"", "P", "PT", "garbage", "PT999999999999999999M", "PT65536M"}) {
    trigger = std::string(text); assert(trigger.Value == 0);
  }
  trigger = std::string("-PT15M"); assert(trigger.Before && trigger.Value == 15);
  trigger = std::string("PT30M"); assert(!trigger.Before && trigger.Value == 30);
  trigger = std::string("PT65535M"); assert(std::string(trigger) == "PT65535M");
  Date date; date = std::string("20261009T143000Z");
  assert(date.Format() == "2026/10/09 14:30");
  assert(std::string(date) == "20261009T143000");
  date[YEAR] = 32767; date[MONTH] = 32767; date[DAY] = 32767;
  date[HOUR] = 32767; date[MINUTE] = 32767; date[SECOND] = 32767;
  assert(!date.Format().empty()); assert(!std::string(date).empty()); date.toUnix();
}

#include "types.h"

Recurrence::operator string() const {
	char Temp[16];
	string Text = "FREQ=";
	switch (Freq) {
		case YEAR: Text += "YEARLY"; break;
		case MONTH: Text += "MONTHLY"; break;
		case DAY: Text += "DAILY"; break;
		case HOUR: Text += "HOURLY"; break;
		case MINUTE: Text += "MINUTELY"; break;
		case SECOND: Text += "SECONDLY"; break;
		case WEEK: Text += "WEEKLY"; break;
	}
	Text += ";INTERVAL=";
	snprintf(Temp, sizeof(Temp), "%d", Interval);
	Text += Temp;
	if (Count > 0) {
		Text += ";COUNT=";
		snprintf(Temp, sizeof(Temp), "%d", Count);
		Text += Temp;
	} else if (!Until.IsEmpty()) {
		Text += ";UNTIL=";
		Text += Until;
	}
	
	return Text;
}

AlarmTrigger &AlarmTrigger::operator =(const string &Text) {
    Before = !Text.empty() && Text.front() == '-';
    Value = 0;
    Unit = MINUTE;
    const size_t prefix = Before ? 1 : 0;
    if (Text.size() <= prefix || Text[prefix] != 'P') return *this;
    const auto start = Text.find_first_of("0123456789", prefix + 1);
    if (start == string::npos) return *this;
    const auto end = Text.find_first_not_of("0123456789", start);
    if (end == string::npos || end - start > 5) return *this;
    const unsigned long value = std::stoul(Text.substr(start, end - start));
    if (value > 65535) return *this;
    switch (Text[end]) {
        case 'H': Unit = HOUR; break;
        case 'D': Unit = DAY; break;
        case 'W': Unit = WEEK; break;
        case 'M': Unit = MINUTE; break;
        default: return *this;
    }
    Value = static_cast<unsigned short>(value);
    return *this;
}

AlarmTrigger::operator string() const {
	string Text;
	char Temp[16];
	
	snprintf(Temp, sizeof(Temp), "%d", Value);
	
	if (Before)
		Text = '-';
	Text += 'P';
	if (Unit != WEEK && Unit != DAY)
		Text += 'T';
	Text += Temp;
	switch (Unit) {
		case HOUR: Text += 'H'; break;
		case DAY: Text += 'D'; break;
		case WEEK: Text += 'W'; break;
		default: Text += 'M'; break;
	}
	
	return Text;
}

Alarm::operator string() const {
	string Text = "\r\nBEGIN:VALARM";
	Text += "\r\nTRIGGER:";
	Text += Trigger;
	Text += "\r\nACTION:";
	switch (Action) {
		case AUDIO: Text += "AUDIO"; break;
		case DISPLAY: Text += "DISPLAY"; break;
		case PROCEDURE: Text += "PROCEDURE"; break;
		case EMAIL: Text += "EMAIL"; break;
	}
	if (!Description.empty()) {
		Text += "\r\nDESCRIPTION:";
		Text += Description;
	}
	Text += "\r\nEND:VALARM";
	
	return Text;
}

ICalendarEvent::operator string() const {
	string Text = "BEGIN:VEVENT";
	Text += "\r\nUID:";
	Text += UID;
	Text += "\r\nSUMMARY:";
	
	Text += Summary;
	// some programs do not add this property, let's leave it when modifying
	if (!DtStamp.IsEmpty()) {
		Text += "\r\nDTSTAMP:";
		Text += DtStamp;
	}
	Text += "\r\nDTSTART:";
	Text += DtStart;
	if (!DtEnd.IsEmpty()) {
		Text += "\r\nDTEND:";
		Text += DtEnd;
	}
	if (!Description.empty()) {
		Text += "\r\nDESCRIPTION:";
		Text += Description;
	}
	if (!Categories.empty()) {
		Text += "\r\nCATEGORIES:";
		Text += Categories;
	}
	if (!RRule.IsEmpty()) {
		Text += "\r\nRRULE:";
		Text += RRule;
	}
	
	if (!Alarms->empty()) {
		for (list<Alarm>::const_iterator AlarmsIterator = Alarms->begin(); AlarmsIterator != Alarms->end(); ++AlarmsIterator) {
			Text += *AlarmsIterator;
		}
	}
	
	Text += "\r\nEND:VEVENT\r\n";
	
	return Text;
}

bool ICalendarEvent::HasAlarm(const Date &From, const Date &To) {
	if (Alarms->empty())
		return false;
	
	bool HasAlarm = false;
	Date AlarmDate;
	
	for (list<Alarm>::iterator AlarmsIterator = Alarms->begin(); AlarmsIterator != Alarms->end(); ++AlarmsIterator) {
		AlarmDate = DtStart;
		
		if ((*AlarmsIterator).Trigger.Before)
			AlarmDate[(*AlarmsIterator).Trigger.Unit] -= (*AlarmsIterator).Trigger.Value;
		else
			AlarmDate[(*AlarmsIterator).Trigger.Unit] += (*AlarmsIterator).Trigger.Value;
		
		if (AlarmDate >= From && AlarmDate <= To) {
			HasAlarm = true;
			(*AlarmsIterator).Active = true;
		} else {
			(*AlarmsIterator).Active = false;
		}
	}
	
	return HasAlarm;
}

// Importations

#include <Arduino.h>
#include <AccelStepper.h> 

// Pins

int xDir = 5; 
int xStep = 2; 
int enable = 8; 

// Variables

int CodeIn;
AccelStepper xStepper(AccelStepper::DRIVER, xStep, xDir); 
int microstepping = 16;

// Conversion
long longueur_to_steps(float longueur) 
{
  
  long steps = (long)(longueur * 200 * microstepping/8) ; // 1 rotation => 8 mm
  return steps;
}

// Syringe pump A : enable the stepper driver (holding torque, ready to move)
// This driver's EN pin is active-LOW: LOW = enabled, HIGH = disabled.
void enable_motor(){
  digitalWrite(enable, LOW);
}

// Syringe pump A : disable the stepper driver (no holding torque) when idle
void disable_motor(){
  digitalWrite(enable, HIGH);
}



void setup() {   
  Serial.begin(115200);
  pinMode(xDir, OUTPUT);
  pinMode(xStep, OUTPUT);
  pinMode(enable, OUTPUT);
  xStepper.setPinsInverted(false, false, true);
  disable_motor(); // Motor is idle at startup

  xStepper.setMaxSpeed(80); // Speed : Steps per seconde
  xStepper.setAcceleration(300); 
  xStepper.setSpeed(80); // Speed : Steps per seconde
  Serial.println("Syringe pump A : ready");
}


// Syringe pump A : go
void to_go()
{   float position = 100;
    long steps = longueur_to_steps(position); // Conversion
    xStepper.move(steps);

}

// Syringe pump A : back off
void to_back_off(){
  float location = -50;
  long steps_location = longueur_to_steps(location);
  xStepper.move(steps_location); // position relative
}

// Syringe pump A : change speed (steps per second)
void set_speed(int speed){
  xStepper.setMaxSpeed(speed);
  xStepper.setSpeed(speed);
}



void loop() 
{
  if (Serial.available() > 0) 
  { 
    char CodeIn = Serial.read();
    if (CodeIn == 'A') { 
      enable_motor(); 
      to_go();}

    if (CodeIn == 'S'){
        xStepper.stop();
      }

    if (CodeIn == 'R'){
        enable_motor();
        to_back_off();
      }
    if (CodeIn == 'V'){
        int newSpeed = Serial.parseInt(); // lit les chiffres qui suivent le 'V', ex: "V80" -> 80
        if (newSpeed > 0) {
          set_speed(newSpeed);
        }
      }
  }

  if (!xStepper.isRunning())
  {
    disable_motor(); // Disable the motor as soon as it is no longer moving
  }
  xStepper.run();
}


